"""S3 - Classify (Extend). In: artifacts (post-explode). Out: one
SheetClassification per artifact.

Classifies the same leaf artifacts S2 parses (S1's sheets, plus any
top-level artifact S1 passed through unchanged). The taxonomy (s7.taxonomy)
is sent as an *inline* classify config on every call rather than referencing
a saved Extend classifier -- see providers/extend.py's create_classify_run
docstring for why: a saved classifier is scoped to one Extend workspace, and
this repo is open source. Sending the taxonomy inline makes classification
fully reproducible from the checked-in source with nothing to provision.

On confidence < CONFIDENCE_THRESHOLD, retries once with the
`classification_performance` base processor instead of the first pass's
cheaper `classification_light` (see taxonomy.py's BASE_PROCESSOR_* constants).
Still below threshold after the retry: the artifact is flagged needs_review
and S5 will skip it.

Results are cached by content hash + taxonomy config hash (the *final*
decision, after any retry) so a re-run -- or a different run that happens to
share a file -- never re-bills Extend.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Engine

from s7.config import Settings, get_settings
from s7.models.stage import StageResult, StageStatus
from s7.providers.extend import ClassifyRun, ExtendClient, ExtendError, config_hash
from s7.store import events
from s7.store.artifacts import list_parse_targets
from s7.store.classifications import (
    delete_classifications_for_run,
    has_classifications,
    insert_classification,
)
from s7.store.db import get_engine
from s7.store.extend_cache import (
    get_cached_classify,
    get_cached_file_id,
    store_classify_cache,
    store_uploaded_file,
)
from s7.store.provider_calls import record_provider_call
from s7.store.runs import get_run, update_run_status
from s7.taxonomy import (
    BASE_PROCESSOR_FIRST_PASS,
    BASE_PROCESSOR_RETRY,
    CONFIDENCE_THRESHOLD,
    classify_config,
)

STAGE = "s3_classify"
MAX_CONCURRENCY = 5
CLASSIFY_TIMEOUT_S = 120.0

# The cache key's "processor_id"/"processor_version" columns predate the move
# to inline config -- kept as-is (no schema migration needed) but repurposed:
# a constant marker plus the taxonomy's own hash, so the DB still records
# exactly what classified each artifact without implying a saved classifier.
_INLINE_PROCESSOR_MARKER = "inline"
_TAXONOMY_HASH = config_hash(classify_config(base_processor=BASE_PROCESSOR_FIRST_PASS))


async def _get_or_upload_file_id(
    engine: Engine, client: ExtendClient, artifact: dict[str, Any]
) -> str:
    sha256 = artifact["sha256"]
    with engine.begin() as conn:
        file_id = get_cached_file_id(conn, sha256)
    if file_id is not None:
        return file_id

    data = Path(str(artifact["storage_path"])).read_bytes()
    file = await client.upload_file(
        file_name=str(artifact["file_name"]), data=data, mime_type=str(artifact["mime_type"])
    )
    with engine.begin() as conn:
        store_uploaded_file(conn, sha256=sha256, file_id=file.id, credits=0.0)
    return file.id


async def _classify_live(client: ExtendClient, *, file_id: str) -> tuple[ClassifyRun, bool]:
    """Dispatch + poll, retrying once with the performance base processor on
    low confidence. Returns (final_run, retried).
    """
    classify_run_id = await client.create_classify_run(
        file_id=file_id, config=classify_config(base_processor=BASE_PROCESSOR_FIRST_PASS)
    )
    run = await client.wait_for_classify_run(classify_run_id, timeout=CLASSIFY_TIMEOUT_S)

    if run.status == "PROCESSED" and run.output and run.output.confidence < CONFIDENCE_THRESHOLD:
        retry_run_id = await client.create_classify_run(
            file_id=file_id, config=classify_config(base_processor=BASE_PROCESSOR_RETRY)
        )
        run = await client.wait_for_classify_run(retry_run_id, timeout=CLASSIFY_TIMEOUT_S)
        return run, True

    return run, False


async def _classify_one(
    engine: Engine,
    client: ExtendClient,
    settings: Settings,
    *,
    run_id: str,
    artifact: dict[str, Any],
    counts: dict[str, int],
    counts_lock: asyncio.Lock,
) -> None:
    artifact_id = artifact["id"]
    sha256 = artifact["sha256"]
    started = time.monotonic()

    try:
        with engine.begin() as conn:
            cached = get_cached_classify(conn, sha256, _TAXONOMY_HASH)

        if cached is not None:
            classification_id = str(cached["classification_id"])
            confidence = float(cached["confidence"])
            insights = str(cached["insights"])
            retried = bool(cached["retried"])
            needs_review = bool(cached["needs_review"])
            credits = 0.0
            cache_hit = True
            ok = True
        else:
            file_id = await _get_or_upload_file_id(engine, client, artifact)
            run, retried = await _classify_live(client, file_id=file_id)
            credits = float(run.usage.credits) if run.usage and run.usage.credits else 0.0
            ok = run.status == "PROCESSED" and run.output is not None
            cache_hit = False

            if not ok:
                with engine.begin() as conn:
                    events.emit(
                        conn,
                        run_id=run_id,
                        stage=STAGE,
                        entity_id=artifact_id,
                        event_type="error",
                        level="error",
                        message=f"classify failed for {artifact['file_name']}: "
                        f"{run.failure_reason or run.status}",
                    )
                    record_provider_call(
                        conn,
                        run_id=run_id,
                        stage=STAGE,
                        provider="extend",
                        operation="classify",
                        cost_credits=credits,
                        cached=False,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        ok=False,
                    )
                async with counts_lock:
                    counts["failed"] += 1
                return

            assert run.output is not None
            classification_id = run.output.id
            confidence = run.output.confidence
            insights = "\n\n".join(i.content for i in run.output.insights) or (
                "(no reasoning returned)"
            )
            needs_review = confidence < CONFIDENCE_THRESHOLD

            with engine.begin() as conn:
                store_classify_cache(
                    conn,
                    sha256=sha256,
                    config_hash=_TAXONOMY_HASH,
                    classification_id=classification_id,
                    confidence=confidence,
                    insights=insights,
                    retried=retried,
                    needs_review=needs_review,
                    credits=credits,
                )

        with engine.begin() as conn:
            insert_classification(
                conn,
                run_id=run_id,
                artifact_id=artifact_id,
                classification_id=classification_id,
                confidence=confidence,
                insights=insights,
                processor_id=_INLINE_PROCESSOR_MARKER,
                processor_version=_TAXONOMY_HASH,
                retried=retried,
                needs_review=needs_review,
            )
            record_provider_call(
                conn,
                run_id=run_id,
                stage=STAGE,
                provider="extend",
                operation="classify",
                cost_credits=credits,
                cached=cache_hit,
                latency_ms=int((time.monotonic() - started) * 1000),
                ok=True,
            )
            sheet_suffix = f" ({artifact['sheet_name']})" if artifact.get("sheet_name") else ""
            events.emit(
                conn,
                run_id=run_id,
                stage=STAGE,
                entity_id=artifact_id,
                event_type="classify_completed",
                message=f"{artifact['file_name']}{sheet_suffix} -> {classification_id} "
                f"({confidence:.2f})",
                payload={
                    "classification_id": classification_id,
                    "confidence": confidence,
                    "retried": retried,
                    "needs_review": needs_review,
                    "cached": cache_hit,
                },
            )
        async with counts_lock:
            counts["classified"] += 1
            if needs_review:
                counts["needs_review"] += 1
            if retried:
                counts["retried"] += 1
            if cache_hit:
                counts["cached"] += 1

    except (ExtendError, httpx.HTTPError, OSError) as exc:
        with engine.begin() as conn:
            events.emit(
                conn,
                run_id=run_id,
                stage=STAGE,
                entity_id=artifact_id,
                event_type="error",
                level="error",
                message=f"classify errored for {artifact['file_name']}: {exc}",
            )
        async with counts_lock:
            counts["failed"] += 1


async def run(run_id: str, *, force: bool = False) -> StageResult:
    settings = get_settings()
    settings.ensure_dirs()
    settings.require("extend_api_key", stage=STAGE)
    engine = get_engine()

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        already = has_classifications(conn, run_id)
        if not force and already:
            return StageResult(stage=STAGE, status="skipped", counts={})
        if force and already:
            delete_classifications_for_run(conn, run_id)

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S3 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)
        targets = list_parse_targets(conn, run_id)

    counts = {"classified": 0, "failed": 0, "needs_review": 0, "retried": 0, "cached": 0}
    counts_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def bounded(artifact: dict[str, Any], client: ExtendClient) -> None:
        async with semaphore:
            await _classify_one(
                engine,
                client,
                settings,
                run_id=run_id,
                artifact=artifact,
                counts=counts,
                counts_lock=counts_lock,
            )

    if targets:
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            client = ExtendClient(settings, stage=STAGE, httpx_client=http_client)
            await asyncio.gather(*(bounded(a, client) for a in targets))

    status: StageStatus = "done" if counts["failed"] == 0 else "partial"
    with engine.begin() as conn:
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S3 classify finished: {counts['classified']} classified "
            f"({counts['needs_review']} needs_review, {counts['retried']} retried, "
            f"{counts['cached']} cached), {counts['failed']} failed",
            payload=counts,
        )

    return StageResult(stage=STAGE, status=status, counts=counts)
