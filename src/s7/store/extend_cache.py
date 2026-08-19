"""Caches for Extend calls, keyed on content so re-running a paper -- even in
a different run, even across papers that happen to share a file -- never
re-uploads or re-bills.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

from s7.store.db import now_iso


def get_cached_file_id(conn: Connection, sha256: str) -> str | None:
    row = conn.execute(
        text("SELECT file_id FROM extend_file_uploads WHERE sha256 = :sha256"), {"sha256": sha256}
    ).first()
    return str(row[0]) if row else None


def store_uploaded_file(conn: Connection, *, sha256: str, file_id: str, credits: float) -> None:
    conn.execute(
        text(
            "INSERT INTO extend_file_uploads (sha256, file_id, credits, uploaded_at) "
            "VALUES (:sha256, :file_id, :credits, :ts) "
            "ON CONFLICT(sha256) DO UPDATE SET file_id = excluded.file_id, "
            "credits = excluded.credits, uploaded_at = excluded.uploaded_at"
        ),
        {"sha256": sha256, "file_id": file_id, "credits": credits, "ts": now_iso()},
    )


def _cache_key(sha256: str, config_hash: str) -> str:
    return f"{sha256}:{config_hash}"


def get_cached_parse(conn: Connection, sha256: str, config_hash: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT * FROM extend_parse_cache WHERE cache_key = :key"),
        {"key": _cache_key(sha256, config_hash)},
    ).first()
    return dict(row._mapping) if row else None


def store_parse_cache(
    conn: Connection,
    *,
    sha256: str,
    config_hash: str,
    parse_run_id: str,
    status: str,
    raw_response_path: str | None,
    credits: float,
) -> None:
    conn.execute(
        text(
            "INSERT INTO extend_parse_cache (cache_key, sha256, config_hash, parse_run_id, "
            "status, raw_response_path, credits, created_at) VALUES "
            "(:key, :sha256, :config_hash, :parse_run_id, :status, :raw_response_path, "
            ":credits, :ts) "
            "ON CONFLICT(cache_key) DO UPDATE SET parse_run_id = excluded.parse_run_id, "
            "status = excluded.status, raw_response_path = excluded.raw_response_path, "
            "credits = excluded.credits, created_at = excluded.created_at"
        ),
        {
            "key": _cache_key(sha256, config_hash),
            "sha256": sha256,
            "config_hash": config_hash,
            "parse_run_id": parse_run_id,
            "status": status,
            "raw_response_path": raw_response_path,
            "credits": credits,
            "ts": now_iso(),
        },
    )


def get_cached_classify(conn: Connection, sha256: str, config_hash: str) -> dict[str, Any] | None:
    """Keyed on the taxonomy's own hash (see s3_classify.py), not a saved
    classifier's id/version -- there is no saved classifier (see
    providers/extend.py's create_classify_run docstring). The key
    deliberately excludes which base_processor a given attempt used: the
    cache stores the *final* decision after any confidence-triggered retry,
    so a cache hit never needs to know how many attempts it took.
    """
    row = conn.execute(
        text("SELECT * FROM extend_classify_cache WHERE cache_key = :key"),
        {"key": _cache_key(sha256, config_hash)},
    ).first()
    return dict(row._mapping) if row else None


def store_classify_cache(
    conn: Connection,
    *,
    sha256: str,
    config_hash: str,
    classification_id: str,
    confidence: float,
    insights: str,
    retried: bool,
    needs_review: bool,
    credits: float,
) -> None:
    conn.execute(
        text(
            "INSERT INTO extend_classify_cache (cache_key, sha256, config_hash, "
            "classification_id, confidence, insights, retried, "
            "needs_review, credits, created_at) VALUES "
            "(:key, :sha256, :config_hash, :classification_id, "
            ":confidence, :insights, :retried, :needs_review, :credits, :ts) "
            "ON CONFLICT(cache_key) DO UPDATE SET classification_id = excluded.classification_id, "
            "confidence = excluded.confidence, insights = excluded.insights, "
            "retried = excluded.retried, needs_review = excluded.needs_review, "
            "credits = excluded.credits, created_at = excluded.created_at"
        ),
        {
            "key": _cache_key(sha256, config_hash),
            "sha256": sha256,
            "config_hash": config_hash,
            "classification_id": classification_id,
            "confidence": confidence,
            "insights": insights,
            "retried": int(retried),
            "needs_review": int(needs_review),
            "credits": credits,
            "ts": now_iso(),
        },
    )
