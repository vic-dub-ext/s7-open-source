"""CRUD for `ontology_cache` -- every gene/variant/trait lookup keyed on the
raw string, shared across runs (and papers) that happen to share a value.
These calls dominate wall-clock time otherwise.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from s7.store.db import new_id, now_iso


def get_cached(conn: Connection, *, kind: str, raw_value: str) -> Any | None:
    row = conn.execute(
        text(
            "SELECT resolved_json FROM ontology_cache WHERE kind = :kind AND raw_value = :raw"
        ),
        {"kind": kind, "raw": raw_value},
    ).first()
    return json.loads(row[0]) if row else None


def store_cached(conn: Connection, *, kind: str, raw_value: str, resolved: Any) -> None:
    """Upsert -- a re-resolution (e.g. after an API fix) should overwrite,
    not conflict.
    """
    conn.execute(
        text(
            "INSERT INTO ontology_cache (id, kind, raw_value, resolved_json, created_at) "
            "VALUES (:id, :kind, :raw, :resolved_json, :ts) "
            "ON CONFLICT(kind, raw_value) DO UPDATE SET "
            "resolved_json = excluded.resolved_json, created_at = excluded.created_at"
        ),
        {
            "id": new_id(),
            "kind": kind,
            "raw": raw_value,
            "resolved_json": json.dumps(resolved),
            "ts": now_iso(),
        },
    )
