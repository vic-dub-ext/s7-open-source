"""CRUD for S3's output: `sheet_classifications`."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from s7.store.db import new_id, now_iso


def insert_classification(
    conn: Connection,
    *,
    run_id: str,
    artifact_id: str,
    classification_id: str,
    confidence: float,
    insights: str,
    processor_id: str,
    processor_version: str,
    retried: bool,
    needs_review: bool,
) -> str:
    row_id = new_id()
    conn.execute(
        text(
            "INSERT INTO sheet_classifications (id, run_id, artifact_id, classification_id, "
            "confidence, insights, processor_id, processor_version, retried, needs_review, "
            "created_at) VALUES (:id, :run_id, :artifact_id, :classification_id, :confidence, "
            ":insights, :processor_id, :processor_version, :retried, :needs_review, :created_at)"
        ),
        {
            "id": row_id,
            "run_id": run_id,
            "artifact_id": artifact_id,
            "classification_id": classification_id,
            "confidence": confidence,
            "insights": insights,
            "processor_id": processor_id,
            "processor_version": processor_version,
            "retried": int(retried),
            "needs_review": int(needs_review),
            "created_at": now_iso(),
        },
    )
    return row_id


def has_classifications(conn: Connection, run_id: str) -> bool:
    count = conn.execute(
        text("SELECT COUNT(*) FROM sheet_classifications WHERE run_id = :run_id"),
        {"run_id": run_id},
    ).scalar_one()
    return bool(count)


def delete_classifications_for_run(conn: Connection, run_id: str) -> None:
    conn.execute(
        text("DELETE FROM sheet_classifications WHERE run_id = :run_id"), {"run_id": run_id}
    )


def list_by_effective_classification(
    conn: Connection, run_id: str, classification_ids: list[str]
) -> list[dict[str, Any]]:
    """Artifacts whose *effective* class (human override, if any, else the
    model's) is one of the given IDs. Used by S4 to find mask/phenotype
    decoder tables and by S5 to select what to induce contracts for.
    """
    placeholders = ", ".join(f":cid{i}" for i in range(len(classification_ids)))
    params: dict[str, Any] = {"run_id": run_id}
    params.update({f"cid{i}": cid for i, cid in enumerate(classification_ids)})
    rows = conn.execute(
        text(
            "SELECT sc.*, a.file_name, a.sheet_name, a.kind AS artifact_kind "
            "FROM sheet_classifications sc JOIN artifacts a ON a.id = sc.artifact_id "
            "WHERE sc.run_id = :run_id "
            f"AND COALESCE(sc.human_override_class, sc.classification_id) IN ({placeholders})"
        ),
        params,
    )
    return [dict(r._mapping) for r in rows]


def list_classifications_for_run(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    """Joined with the artifact so the inspector can show file/sheet name without
    a second round trip. Ordered ascending by confidence -- reviewers spend
    most of their time at the bottom of that list.
    """
    rows = conn.execute(
        text(
            "SELECT sc.*, a.file_name, a.sheet_name, a.kind AS artifact_kind "
            "FROM sheet_classifications sc JOIN artifacts a ON a.id = sc.artifact_id "
            "WHERE sc.run_id = :run_id ORDER BY sc.confidence ASC"
        ),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]


def get_classification(conn: Connection, classification_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text(
            "SELECT sc.*, a.file_name, a.sheet_name, a.kind AS artifact_kind "
            "FROM sheet_classifications sc JOIN artifacts a ON a.id = sc.artifact_id "
            "WHERE sc.id = :id"
        ),
        {"id": classification_id},
    ).first()
    return dict(row._mapping) if row else None


def set_human_override(conn: Connection, classification_id: str, override_class: str) -> None:
    conn.execute(
        text(
            "UPDATE sheet_classifications SET human_override_class = :cls WHERE id = :id"
        ),
        {"cls": override_class, "id": classification_id},
    )


def bucket_counts_for_run(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    """Per-taxonomy-bucket counts for a run (effective class -- a human
    override wins over the model's own), plus how many of each bucket are
    needs_review. Feeds the S3 bucket chart and the runs list's
    distribution bar -- both read from this one query rather than each
    re-deriving it.
    """
    rows = conn.execute(
        text(
            "SELECT COALESCE(human_override_class, classification_id) AS bucket, "
            "COUNT(*) AS count, "
            "SUM(CASE WHEN needs_review THEN 1 ELSE 0 END) AS needs_review_count "
            "FROM sheet_classifications WHERE run_id = :run_id GROUP BY bucket"
        ),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]
