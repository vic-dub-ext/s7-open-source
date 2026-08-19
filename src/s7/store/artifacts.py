"""CRUD for the `artifacts` table -- S0's downloads and S1's exploded sheets."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from s7.store.db import new_id


def insert_artifact(
    conn: Connection,
    *,
    run_id: str,
    kind: str,
    file_name: str,
    mime_type: str,
    byte_size: int,
    sha256: str,
    storage_path: str,
    retrieved_at: str,
    download_url: str | None = None,
    parent_artifact_id: str | None = None,
    sheet_name: str | None = None,
    row_offset: int | None = None,
    col_offset: int | None = None,
    row_count: int | None = None,
    col_count: int | None = None,
    is_classify_sample: bool = False,
    skip_reason: str | None = None,
    skip_detail: str | None = None,
) -> str:
    artifact_id = new_id()
    conn.execute(
        text(
            "INSERT INTO artifacts (id, run_id, kind, file_name, mime_type, byte_size, "
            "sha256, download_url, retrieved_at, storage_path, parent_artifact_id, "
            "sheet_name, row_offset, col_offset, row_count, col_count, is_classify_sample, "
            "skip_reason, skip_detail) VALUES "
            "(:id, :run_id, :kind, :file_name, :mime_type, :byte_size, :sha256, "
            ":download_url, :retrieved_at, :storage_path, :parent_artifact_id, "
            ":sheet_name, :row_offset, :col_offset, :row_count, :col_count, "
            ":is_classify_sample, :skip_reason, :skip_detail)"
        ),
        {
            "id": artifact_id,
            "run_id": run_id,
            "kind": kind,
            "file_name": file_name,
            "mime_type": mime_type,
            "byte_size": byte_size,
            "sha256": sha256,
            "download_url": download_url,
            "retrieved_at": retrieved_at,
            "storage_path": storage_path,
            "parent_artifact_id": parent_artifact_id,
            "sheet_name": sheet_name,
            "row_offset": row_offset,
            "col_offset": col_offset,
            "row_count": row_count,
            "col_count": col_count,
            "is_classify_sample": int(is_classify_sample),
            "skip_reason": skip_reason,
            "skip_detail": skip_detail,
        },
    )
    return artifact_id


def get_artifact(conn: Connection, artifact_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT * FROM artifacts WHERE id = :id"), {"id": artifact_id}
    ).first()
    return dict(row._mapping) if row else None


def list_top_level(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT * FROM artifacts WHERE run_id = :run_id AND parent_artifact_id IS NULL "
            "ORDER BY kind, file_name"
        ),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]


def list_children(conn: Connection, parent_artifact_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT * FROM artifacts WHERE parent_artifact_id = :parent_id "
            "ORDER BY sheet_name"
        ),
        {"parent_id": parent_artifact_id},
    )
    return [dict(r._mapping) for r in rows]


def list_parse_targets(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    """The artifacts S2 actually parses: every exploded sheet, plus every
    top-level artifact S1 passed through unchanged (PDFs, docx). Top-level
    artifacts that WERE exploded (their xlsx/csv content lives in the sheet
    children instead) are excluded, as are skipped artifacts.
    """
    rows = conn.execute(
        text(
            "SELECT * FROM artifacts WHERE run_id = :run_id AND skip_reason IS NULL "
            "AND (parent_artifact_id IS NOT NULL OR id NOT IN ("
            "  SELECT DISTINCT parent_artifact_id FROM artifacts "
            "  WHERE run_id = :run_id AND parent_artifact_id IS NOT NULL"
            ")) ORDER BY kind, file_name, sheet_name"
        ),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]


def has_children(conn: Connection, run_id: str) -> bool:
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM artifacts WHERE run_id = :run_id "
            "AND parent_artifact_id IS NOT NULL"
        ),
        {"run_id": run_id},
    ).scalar_one()
    return bool(count)


def delete_children_for_run(conn: Connection, run_id: str) -> None:
    """Used by S1's force re-run -- clears prior exploded sheets without
    touching S0's top-level downloads.
    """
    conn.execute(
        text(
            "DELETE FROM artifacts WHERE run_id = :run_id AND parent_artifact_id IS NOT NULL"
        ),
        {"run_id": run_id},
    )


def delete_all_for_run(conn: Connection, run_id: str) -> None:
    """Used by S0's force re-run -- children first, since parent_artifact_id
    is a foreign key and rows are deleted with foreign_keys=ON.
    """
    delete_children_for_run(conn, run_id)
    conn.execute(
        text("DELETE FROM artifacts WHERE run_id = :run_id AND parent_artifact_id IS NULL"),
        {"run_id": run_id},
    )


def find_by_sha256(conn: Connection, run_id: str, sha256: str) -> dict[str, Any] | None:
    """Content-addressed dedup check -- re-running a paper must not re-download."""
    row = conn.execute(
        text("SELECT * FROM artifacts WHERE run_id = :run_id AND sha256 = :sha256 LIMIT 1"),
        {"run_id": run_id, "sha256": sha256},
    ).first()
    return dict(row._mapping) if row else None
