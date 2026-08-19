"""CRUD for S8's output: `check_results`. S8 only ever inserts (each check
run produces new rows); S9 reads them back to compute confidence and
review_status.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from s7.store.db import new_id, now_iso


def insert_check_results(conn: Connection, results: list[dict[str, Any]]) -> None:
    """Each dict needs record_id, check_name, status, detail, checked_by."""
    if not results:
        return
    rows = [
        {
            "id": new_id(),
            "record_id": r["record_id"],
            "check_name": r["check_name"],
            "status": r["status"],
            "detail": r["detail"],
            "checked_by": r["checked_by"],
            "created_at": now_iso(),
        }
        for r in results
    ]
    conn.execute(
        text(
            "INSERT INTO check_results (id, record_id, check_name, status, detail, "
            "checked_by, created_at) VALUES "
            "(:id, :record_id, :check_name, :status, :detail, :checked_by, :created_at)"
        ),
        rows,
    )


def has_checks_for_run(conn: Connection, run_id: str) -> bool:
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM check_results cr "
            "JOIN association_records ar ON ar.record_id = cr.record_id "
            "WHERE ar.run_id = :run_id"
        ),
        {"run_id": run_id},
    ).scalar_one()
    return bool(count)


def delete_checks_for_run(conn: Connection, run_id: str) -> None:
    conn.execute(
        text(
            "DELETE FROM check_results WHERE record_id IN "
            "(SELECT record_id FROM association_records WHERE run_id = :run_id)"
        ),
        {"run_id": run_id},
    )


def list_check_results_for_record(conn: Connection, record_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT * FROM check_results WHERE record_id = :id ORDER BY created_at"
        ),
        {"id": record_id},
    )
    return [dict(r._mapping) for r in rows]


def list_check_results_for_run(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    """Joined for a single scan -- S9 arbitration reads every check result
    for a run at once rather than per-record.
    """
    rows = conn.execute(
        text(
            "SELECT cr.* FROM check_results cr "
            "JOIN association_records ar ON ar.record_id = cr.record_id "
            "WHERE ar.run_id = :run_id"
        ),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]
