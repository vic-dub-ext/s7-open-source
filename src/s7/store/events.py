"""The structured event log. Every stage emits here; the UI's log stream and
per-stage counts are both just reads of this one table. Log the *decision*,
not just the action -- e.g. classify_completed carries class + confidence +
reasoning, so the log alone explains why.
"""

from __future__ import annotations

import json
from typing import Any, Literal

import structlog
from sqlalchemy import Connection, text

from s7.store.db import now_iso

logger = structlog.get_logger()

Level = Literal["debug", "info", "warn", "error"]

# The minimum event types every stage relies on. Stages may emit others as needed.
EVENT_TYPES = (
    "stage_started",
    "stage_finished",
    "artifact_found",
    "artifact_skipped",
    "parse_dispatched",
    "parse_completed",
    "classify_completed",
    "contract_induced",
    "contract_disagreement",
    "projection_completed",
    "check_failed",
    "record_quarantined",
    "external_call",
    "error",
)


def emit(
    conn: Connection,
    *,
    event_type: str,
    message: str,
    run_id: str | None = None,
    stage: str | None = None,
    entity_id: str | None = None,
    level: Level = "info",
    payload: dict[str, Any] | None = None,
) -> None:
    """Write one event row and mirror it to structlog for console/CI visibility."""
    conn.execute(
        text(
            "INSERT INTO events (run_id, stage, entity_id, level, event_type, message, "
            "payload_json, ts) VALUES (:run_id, :stage, :entity_id, :level, :event_type, "
            ":message, :payload_json, :ts)"
        ),
        {
            "run_id": run_id,
            "stage": stage,
            "entity_id": entity_id,
            "level": level,
            "event_type": event_type,
            "message": message,
            "payload_json": json.dumps(payload) if payload is not None else None,
            "ts": now_iso(),
        },
    )
    log = getattr(logger, level if level != "warn" else "warning")
    log(
        event_type,
        run_id=run_id,
        stage=stage,
        entity_id=entity_id,
        message=message,
        **(payload or {}),
    )


def has_stage_finished(conn: Connection, run_id: str, stage: str) -> bool:
    """Idempotency signal for stages that mutate existing rows rather than
    owning a dedicated output table (e.g. S7 normalization UPDATEs
    association_records; there's no has_X_for_run to check row existence
    against). A completed run always emits stage_finished, so its presence
    is a clean "did this stage already run" marker.
    """
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM events WHERE run_id = :run_id AND stage = :stage "
            "AND event_type = 'stage_finished'"
        ),
        {"run_id": run_id, "stage": stage},
    ).scalar_one()
    return bool(count)


def list_for_run(
    conn: Connection, run_id: str, *, stage: str | None = None, limit: int = 500
) -> list[dict[str, Any]]:
    if stage:
        rows = conn.execute(
            text(
                "SELECT * FROM events WHERE run_id = :run_id AND stage = :stage "
                "ORDER BY id DESC LIMIT :limit"
            ),
            {"run_id": run_id, "stage": stage, "limit": limit},
        )
    else:
        rows = conn.execute(
            text("SELECT * FROM events WHERE run_id = :run_id ORDER BY id DESC LIMIT :limit"),
            {"run_id": run_id, "limit": limit},
        )
    return [dict(r._mapping) for r in rows]


def since(
    conn: Connection, run_id: str, *, after_id: int = 0, limit: int = 200
) -> list[dict[str, Any]]:
    """Used by the SSE log stream to poll for new rows."""
    rows = conn.execute(
        text(
            "SELECT * FROM events WHERE run_id = :run_id AND id > :after_id "
            "ORDER BY id ASC LIMIT :limit"
        ),
        {"run_id": run_id, "after_id": after_id, "limit": limit},
    )
    return [dict(r._mapping) for r in rows]
