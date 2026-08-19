"""Provider-agnostic chat interface. No model strings are hardcoded in stage
modules -- they always come from a ModelSpec built in s7.config.

Every call returns an LLMCallRecord (model, prompt hash, full prompt, full
response, token counts, latency, cost estimate, stage + entity) so the caller
can persist it -- the UI's contract inspector shows exactly what was asked
and answered. This module never touches the database itself, matching the
pattern in providers/extend.py: the provider wrapper returns data, the
calling stage records it (see store/provider_calls.py for the analogous
Extend-side log).

Both providers enforce the response schema server-side (Anthropic's
`output_config.format`, OpenAI's Responses API `text_format`), so a
`response_model` mismatch should be rare. Retries are for the residual
cases: refusal, truncation, or a transient API error.
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from typing import Any, TypeVar

import anthropic
import openai
from pydantic import BaseModel

from s7.config import ModelSpec, Settings

ResponseModel = TypeVar("ResponseModel", bound=BaseModel)

MAX_TOKENS = 16_000

# $ per million tokens, (input, output). Only models this project actually
# configures by default -- an unlisted model yields cost_usd=None rather than
# a guessed number. Anthropic prices verified against platform.claude.com
# pricing; OpenAI prices verified against developers.openai.com/api/docs/pricing
# (2026-08-17). Both providers' cached-input price works out to 0.1x the base
# input price, so a single _CACHE_READ_MULTIPLIER covers both.
_PRICE_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-sonnet-5": (3.00, 15.00),
    "claude-opus-5": (5.00, 25.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "gpt-5": (1.25, 10.00),
    "gpt-5.1": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
}


class LLMCallRecord(BaseModel):
    model_spec: ModelSpec
    prompt_hash: str
    prompt: str
    response: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    latency_ms: int
    stage: str
    entity_id: str | None = None
    ok: bool = True
    created_at: datetime


class LLMError(Exception):
    """Raised when a call still fails after `max_retries` -- either the
    provider never returned schema-valid JSON, or the API call itself kept
    erroring. Carries the final LLMCallRecord (ok=False) so the caller can
    persist it and mark the entity needs_review; this module never falls
    back to a free-text parse.
    """

    def __init__(self, message: str, record: LLMCallRecord) -> None:
        super().__init__(message)
        self.record = record


def _prompt_hash(system: str, user: str) -> str:
    return hashlib.sha256(f"{system}\n---\n{user}".encode()).hexdigest()


# Anthropic's documented cache multipliers on the *base input* price (see
# shared/prompt-caching.md): a cache write costs 1.25x, a cache read ~0.1x.
# OpenAI's automatic caching (no code needed to enable it) publishes an
# explicit cached-input price that also works out to 0.1x base input for
# every model in _PRICE_PER_MTOK -- but unlike Anthropic, OpenAI's
# `cached_tokens` is a *subset* of `input_tokens`, not additive to it.
_CACHE_WRITE_MULTIPLIER = 1.25  # Anthropic only -- OpenAI has no write charge
_CACHE_READ_MULTIPLIER = 0.10  # both providers


def _estimate_cost_usd(
    model: str,
    input_tokens: int | None,
    output_tokens: int | None,
    *,
    provider: str = "anthropic",
    cache_creation_input_tokens: int | None = None,
    cache_read_input_tokens: int | None = None,
) -> float | None:
    prices = _PRICE_PER_MTOK.get(model)
    if prices is None or input_tokens is None or output_tokens is None:
        return None
    in_price, out_price = prices

    billable_input_tokens = input_tokens
    cost = 0.0
    if provider == "openai" and cache_read_input_tokens:
        # cached_tokens is a subset of input_tokens for OpenAI -- carve it
        # out of the base-price bucket before charging it at the cache rate.
        billable_input_tokens = input_tokens - cache_read_input_tokens
        cost += (cache_read_input_tokens / 1_000_000) * in_price * _CACHE_READ_MULTIPLIER

    cost += (billable_input_tokens / 1_000_000) * in_price
    cost += (output_tokens / 1_000_000) * out_price

    if provider == "anthropic":
        if cache_creation_input_tokens:
            cost += (
                (cache_creation_input_tokens / 1_000_000) * in_price * _CACHE_WRITE_MULTIPLIER
            )
        if cache_read_input_tokens:
            cost += (cache_read_input_tokens / 1_000_000) * in_price * _CACHE_READ_MULTIPLIER

    return cost


class _RawCall(BaseModel):
    """Internal: what a provider call returns before it's wrapped as an LLMCallRecord."""

    model_config = {"arbitrary_types_allowed": True}

    parsed: Any
    raw_text: str
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_input_tokens: int | None = None
    cache_read_input_tokens: int | None = None


# Haiku rejects `thinking: {type: "adaptive"}` outright (400 "adaptive
# thinking is not supported on this model") -- only the Sonnet/Opus tiers
# support it.
_NO_ADAPTIVE_THINKING = {"claude-haiku-4-5"}


async def _call_anthropic(
    *,
    settings: Settings,
    model: ModelSpec,
    system: str,
    user: str,
    response_model: type[ResponseModel],
) -> _RawCall:
    api_key = settings.require("anthropic_api_key", stage="llm:anthropic")
    client = anthropic.AsyncAnthropic(api_key=api_key)
    extra: dict[str, Any] = (
        {} if model.model in _NO_ADAPTIVE_THINKING else {"thinking": {"type": "adaptive"}}
    )
    response = await client.messages.parse(
        model=model.model,
        max_tokens=MAX_TOKENS,
        # Marking the whole system prompt cacheable is a no-op for callers
        # whose system prompt is short (below the ~1024-token minimum, this
        # silently just doesn't cache -- no error, no premium) and a real
        # win for callers who put large, call-to-call-identical context in
        # system (e.g. s8_validate.py's V3 methods excerpt, sent on every
        # one of 1000+ calls for a run).
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
        output_format=response_model,
        **extra,
    )
    raw_text = "".join(block.text for block in response.content if block.type == "text")
    usage = response.usage
    return _RawCall(
        parsed=response.parsed_output,
        raw_text=raw_text,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cache_creation_input_tokens=usage.cache_creation_input_tokens,
        cache_read_input_tokens=usage.cache_read_input_tokens,
    )


async def _call_openai(
    *,
    settings: Settings,
    model: ModelSpec,
    system: str,
    user: str,
    response_model: type[ResponseModel],
) -> _RawCall:
    api_key = settings.require("openai_api_key", stage="llm:openai")
    client = openai.AsyncOpenAI(api_key=api_key)
    response = await client.responses.parse(
        model=model.model,
        instructions=system,
        input=user,
        text_format=response_model,
        max_output_tokens=MAX_TOKENS,
    )
    usage = response.usage
    cached_tokens = (
        usage.input_tokens_details.cached_tokens
        if usage and usage.input_tokens_details
        else None
    )
    return _RawCall(
        parsed=response.output_parsed,
        raw_text=response.output_text,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        cache_read_input_tokens=cached_tokens,
    )


async def complete_json(
    *,
    settings: Settings,
    model: ModelSpec,
    system: str,
    user: str,
    response_model: type[ResponseModel],
    stage: str,
    entity_id: str | None = None,
    max_retries: int = 1,
) -> tuple[ResponseModel, LLMCallRecord]:
    """Call `model`, parse+validate its JSON reply as `response_model`.

    On failure to produce schema-valid output, retries once with the error
    appended to the prompt; on a second failure, raises LLMError carrying
    the failed LLMCallRecord so the caller can persist it and mark the
    entity needs_review. Never falls back to a free-text parse.
    """
    if model.provider == "anthropic":
        call = _call_anthropic
    elif model.provider == "openai":
        call = _call_openai
    else:
        raise ValueError(f"unknown LLM provider: {model.provider!r}")

    current_user = user
    attempt = 0
    while True:
        started = time.monotonic()
        try:
            result = await call(
                settings=settings,
                model=model,
                system=system,
                user=current_user,
                response_model=response_model,
            )
            failure_reason = None if result.parsed is not None else "model returned no valid JSON"
        except Exception as exc:  # noqa: BLE001 -- heterogeneous provider errors, always retried/recorded
            result = None
            failure_reason = str(exc)

        latency_ms = int((time.monotonic() - started) * 1000)
        raw_text = result.raw_text if result is not None else f"<call failed: {failure_reason}>"
        input_tokens = result.input_tokens if result is not None else None
        output_tokens = result.output_tokens if result is not None else None
        cache_creation_input_tokens = (
            result.cache_creation_input_tokens if result is not None else None
        )
        cache_read_input_tokens = result.cache_read_input_tokens if result is not None else None
        record = LLMCallRecord(
            model_spec=model,
            prompt_hash=_prompt_hash(system, current_user),
            prompt=f"### SYSTEM\n{system}\n\n### USER\n{current_user}",
            response=raw_text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cost_usd=_estimate_cost_usd(
                model.model,
                input_tokens,
                output_tokens,
                provider=model.provider,
                cache_creation_input_tokens=cache_creation_input_tokens,
                cache_read_input_tokens=cache_read_input_tokens,
            ),
            latency_ms=latency_ms,
            stage=stage,
            entity_id=entity_id,
            ok=failure_reason is None,
            created_at=datetime.now(UTC),
        )

        if failure_reason is None:
            assert result is not None
            return result.parsed, record

        if attempt >= max_retries:
            raise LLMError(failure_reason, record)

        attempt += 1
        current_user = (
            f"{user}\n\n---\nYour previous response failed: {failure_reason}\n"
            "Return only JSON matching the required schema."
        )
