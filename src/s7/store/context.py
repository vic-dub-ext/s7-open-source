"""CRUD for S4's output: `methods_bundles`."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from s7.store.db import new_id, now_iso


def insert_methods_bundle(
    conn: Connection,
    *,
    run_id: str,
    content: str,
    token_count: int,
    source_artifact_ids: list[str],
) -> str:
    bundle_id = new_id()
    conn.execute(
        text(
            "INSERT INTO methods_bundles (id, run_id, content, token_count, "
            "source_artifact_ids_json, created_at) VALUES "
            "(:id, :run_id, :content, :token_count, :source_artifact_ids_json, :created_at)"
        ),
        {
            "id": bundle_id,
            "run_id": run_id,
            "content": content,
            "token_count": token_count,
            "source_artifact_ids_json": json.dumps(source_artifact_ids),
            "created_at": now_iso(),
        },
    )
    return bundle_id


def get_methods_bundle_for_run(conn: Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            "SELECT * FROM methods_bundles WHERE run_id = :run_id "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"run_id": run_id},
    ).first()
    return dict(row._mapping) if row else None


def has_methods_bundle(conn: Connection, run_id: str) -> bool:
    return get_methods_bundle_for_run(conn, run_id) is not None


def delete_methods_bundles_for_run(conn: Connection, run_id: str) -> None:
    conn.execute(text("DELETE FROM methods_bundles WHERE run_id = :run_id"), {"run_id": run_id})
