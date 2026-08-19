"""Thin async wrapper around the Extend SDK (`extend-ai` on PyPI). Nothing
outside this module imports `extend_ai` directly.

Extend is used for exactly two things: parsing (S2) and classification (S3).
Not extraction, splitting, or editing.

- Async endpoints throughout (parse_runs.create + poll), so the run ID is
  captured immediately and a crashed process can resume by polling.
- Token-bucket rate limiter, configurable via Settings.extend_rate_limit_per_sec.
- Caching (by artifact SHA-256, and separately by SHA-256 + config hash) lives
  in s7.store.parsed, not here -- this module only talks to Extend.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any

import httpx
from extend_ai import AsyncExtend
from extend_ai.core.api_error import ApiError
from extend_ai.types import ClassifyRun, ParseRun
from extend_ai.types import File as ExtendFile
from extend_ai.wrapper.polling import PollingOptions, PollingTimeoutError, poll_until_done_async

from s7.config import Settings

__all__ = [
    "ClassifyRun",
    "ExtendClient",
    "ExtendError",
    "ExtendFile",
    "ParseRun",
    "TokenBucket",
    "config_hash",
]

PROCESSED = "PROCESSED"
FAILED = "FAILED"
TERMINAL_STATUSES = (PROCESSED, FAILED)


class ExtendError(Exception):
    """A parse or classify run that ended in a state we can't use, or an
    Extend API call that failed outright (e.g. a 400 for a file type Extend
    doesn't support -- see `upload_file`). Callers already handle this
    uniformly as one failed artifact rather than a crash of the whole
    batch; letting the SDK's own `ApiError` subclasses (BadRequestError,
    etc.) escape unwrapped would defeat that, which is exactly what
    happened with a real paper's .tsv supplementary file before this
    wrapping was added.
    """


def config_hash(config: dict[str, Any]) -> str:
    """Stable hash of a parse config, used as half of the cache key (the
    other half is the artifact's content SHA-256).
    """
    canonical = json.dumps(config, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class TokenBucket:
    """Simple async token-bucket rate limiter. One `acquire()` per call made
    to Extend, regardless of endpoint.
    """

    def __init__(self, rate_per_sec: float) -> None:
        self._rate = max(rate_per_sec, 0.1)
        self._capacity = max(self._rate, 1.0)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                elapsed = now - self._updated
                self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                await asyncio.sleep((1 - self._tokens) / self._rate)


class ExtendClient:
    """One instance per stage run. Construct with an httpx.AsyncClient the
    caller owns (and closes) so connection pooling is explicit, matching the
    pattern used in s7.stages.s0_acquire.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        stage: str,
        httpx_client: httpx.AsyncClient | None = None,
    ) -> None:
        api_key = settings.require("extend_api_key", stage=stage)
        self._sdk = AsyncExtend(token=api_key, httpx_client=httpx_client)
        self._bucket = TokenBucket(settings.extend_rate_limit_per_sec)

    async def upload_file(self, *, file_name: str, data: bytes, mime_type: str) -> ExtendFile:
        """Uploads are free; credit cost is only incurred by the parse run itself."""
        await self._bucket.acquire()
        try:
            return await self._sdk.files.upload(file=(file_name, data, mime_type))
        except ApiError as exc:
            raise ExtendError(f"upload of {file_name!r} rejected by Extend: {exc}") from exc

    async def create_parse_run(self, *, file_id: str, config: dict[str, Any]) -> str:
        await self._bucket.acquire()
        try:
            run = await self._sdk.parse_runs.create(file={"id": file_id}, config=config)  # type: ignore[arg-type]
        except ApiError as exc:
            raise ExtendError(f"could not create parse run for file {file_id}: {exc}") from exc
        return run.id

    async def get_parse_run(self, parse_run_id: str) -> ParseRun:
        await self._bucket.acquire()
        try:
            return await self._sdk.parse_runs.retrieve(parse_run_id)
        except ApiError as exc:
            raise ExtendError(f"could not retrieve parse run {parse_run_id}: {exc}") from exc

    async def wait_for_parse_run(self, parse_run_id: str, *, timeout: float = 300.0) -> ParseRun:
        """Poll using the SDK's own hybrid fast-poll/backoff strategy (see
        extend_ai.wrapper.polling), so we don't reimplement it. `create_parse_run`
        already persisted `parse_run_id` before this is called, so a crashed
        process resumes by polling rather than re-dispatching the run.
        """
        try:
            run: ParseRun = await poll_until_done_async(
                retrieve=lambda: self.get_parse_run(parse_run_id),
                is_terminal=lambda r: r.status in TERMINAL_STATUSES,
                options=PollingOptions(max_wait_ms=int(timeout * 1000)),
            )
        except PollingTimeoutError as exc:
            raise ExtendError(
                f"parse run {parse_run_id} did not finish within {timeout}s"
            ) from exc
        return run

    async def parse_and_wait(
        self,
        *,
        file_name: str,
        data: bytes,
        mime_type: str,
        config: dict[str, Any],
        timeout: float = 300.0,
    ) -> tuple[ParseRun, float]:
        """Upload + dispatch + poll to completion. Returns (run, credits)."""
        file = await self.upload_file(file_name=file_name, data=data, mime_type=mime_type)
        parse_run_id = await self.create_parse_run(file_id=file.id, config=config)
        run = await self.wait_for_parse_run(parse_run_id, timeout=timeout)
        credits = float(run.usage.credits) if run.usage and run.usage.credits else 0.0
        return run, credits

    async def create_classify_run(self, *, file_id: str, config: dict[str, Any]) -> str:
        """`config` is an inline classify configuration (classifications +
        classification_rules + base_processor), not a reference to a saved
        classifier -- deliberately. A saved classifier is an object scoped
        to one Extend workspace; anyone who clones this repo with their own
        EXTEND_API_KEY has no way to reach a classifier ID that lives in
        someone else's account, and there was no code path in this repo
        that could recreate it. Sending the taxonomy inline on every call
        (see taxonomy.py) makes classification fully reproducible from the
        checked-in source alone -- see the Classify Runs endpoint docs:
        "Reference to an existing classifier. One of `classifier` or
        `config` must be provided."
        """
        await self._bucket.acquire()
        try:
            run = await self._sdk.classify_runs.create(
                file={"id": file_id},
                config=config,  # type: ignore[arg-type]
            )
        except ApiError as exc:
            raise ExtendError(f"could not create classify run for file {file_id}: {exc}") from exc
        return run.id

    async def get_classify_run(self, classify_run_id: str) -> ClassifyRun:
        await self._bucket.acquire()
        try:
            return await self._sdk.classify_runs.retrieve(classify_run_id)
        except ApiError as exc:
            raise ExtendError(f"could not retrieve classify run {classify_run_id}: {exc}") from exc

    async def wait_for_classify_run(
        self, classify_run_id: str, *, timeout: float = 120.0
    ) -> ClassifyRun:
        try:
            run: ClassifyRun = await poll_until_done_async(
                retrieve=lambda: self.get_classify_run(classify_run_id),
                is_terminal=lambda r: r.status in TERMINAL_STATUSES,
                options=PollingOptions(max_wait_ms=int(timeout * 1000)),
            )
        except PollingTimeoutError as exc:
            raise ExtendError(
                f"classify run {classify_run_id} did not finish within {timeout}s"
            ) from exc
        return run
