from __future__ import annotations

import pytest
from pydantic import BaseModel

from s7.config import ModelSpec, Settings
from s7.providers import llm as llm_module
from s7.providers.llm import LLMError, _RawCall, complete_json


class Answer(BaseModel):
    value: str


@pytest.fixture
def settings() -> Settings:
    return Settings(anthropic_api_key="test-key")


@pytest.fixture
def model() -> ModelSpec:
    return ModelSpec(provider="anthropic", model="claude-sonnet-5")


async def test_complete_json_returns_parsed_model_and_ok_record(
    monkeypatch, settings, model
) -> None:
    async def fake_call(**kwargs):
        return _RawCall(
            parsed=Answer(value="ok"), raw_text='{"value": "ok"}', input_tokens=10, output_tokens=5
        )

    monkeypatch.setattr(llm_module, "_call_anthropic", fake_call)

    parsed, record = await complete_json(
        settings=settings,
        model=model,
        system="sys",
        user="usr",
        response_model=Answer,
        stage="test_stage",
        entity_id="entity-1",
    )

    assert parsed == Answer(value="ok")
    assert record.ok is True
    assert record.stage == "test_stage"
    assert record.entity_id == "entity-1"
    assert record.input_tokens == 10
    assert record.output_tokens == 5
    assert record.cost_usd is not None


async def test_complete_json_retries_once_then_succeeds(monkeypatch, settings, model) -> None:
    calls = []

    async def fake_call(*, user, **kwargs):
        calls.append(user)
        if len(calls) == 1:
            return _RawCall(parsed=None, raw_text="not json", input_tokens=1, output_tokens=1)
        return _RawCall(
            parsed=Answer(value="ok"), raw_text='{"value": "ok"}', input_tokens=1, output_tokens=1
        )

    monkeypatch.setattr(llm_module, "_call_anthropic", fake_call)

    parsed, record = await complete_json(
        settings=settings,
        model=model,
        system="sys",
        user="usr",
        response_model=Answer,
        stage="test_stage",
        max_retries=1,
    )

    assert parsed == Answer(value="ok")
    assert record.ok is True
    assert len(calls) == 2
    assert "usr" in calls[0]
    assert "failed" in calls[1]


async def test_complete_json_raises_llm_error_after_exhausting_retries(
    monkeypatch, settings, model
) -> None:
    async def fake_call(**kwargs):
        return _RawCall(parsed=None, raw_text="still not json", input_tokens=1, output_tokens=1)

    monkeypatch.setattr(llm_module, "_call_anthropic", fake_call)

    with pytest.raises(LLMError) as exc_info:
        await complete_json(
            settings=settings,
            model=model,
            system="sys",
            user="usr",
            response_model=Answer,
            stage="test_stage",
            max_retries=1,
        )

    assert exc_info.value.record.ok is False
    assert exc_info.value.record.response == "still not json"


async def test_complete_json_records_exception_and_retries(monkeypatch, settings, model) -> None:
    calls = 0

    async def fake_call(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient error")
        return _RawCall(
            parsed=Answer(value="ok"), raw_text='{"value": "ok"}', input_tokens=1, output_tokens=1
        )

    monkeypatch.setattr(llm_module, "_call_anthropic", fake_call)

    parsed, record = await complete_json(
        settings=settings,
        model=model,
        system="sys",
        user="usr",
        response_model=Answer,
        stage="test_stage",
        max_retries=1,
    )

    assert parsed == Answer(value="ok")
    assert calls == 2


def test_estimate_cost_usd_returns_none_for_unknown_model() -> None:
    assert llm_module._estimate_cost_usd("some-unlisted-model", 100, 100) is None


def test_estimate_cost_usd_computes_known_model() -> None:
    cost = llm_module._estimate_cost_usd("claude-sonnet-5", 1_000_000, 1_000_000)
    assert cost == pytest.approx(3.00 + 15.00)
