"""CRUD for the `runs` table. Small and separate from events.py because both
the CLI and the UI need "list runs" / "create run" without pulling in the
event-log helpers.
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from s7 import __version__
from s7.store import events
from s7.store.db import new_id, now_iso


def create_run(conn: Connection, *, paper_key: str, doi: str, pmcid: str | None = None) -> str:
    run_id = new_id()
    ts = now_iso()
    conn.execute(
        text(
            "INSERT INTO runs (id, paper_key, doi, pmcid, pipeline_version, status, "
            "stage_reached, created_at, updated_at) VALUES "
            "(:id, :paper_key, :doi, :pmcid, :pipeline_version, 'pending', NULL, :ts, :ts)"
        ),
        {
            "id": run_id,
            "paper_key": paper_key,
            "doi": doi,
            "pmcid": pmcid,
            "pipeline_version": __version__,
            "ts": ts,
        },
    )
    return run_id


def list_runs(conn: Connection) -> list[dict[str, Any]]:
    rows = conn.execute(text("SELECT * FROM runs ORDER BY created_at DESC"))
    return [dict(r._mapping) for r in rows]


def get_run(conn: Connection, run_id: str) -> dict[str, Any] | None:
    row = conn.execute(text("SELECT * FROM runs WHERE id = :id"), {"id": run_id}).first()
    return dict(row._mapping) if row else None


def set_pmcid(conn: Connection, run_id: str, pmcid: str) -> None:
    conn.execute(
        text("UPDATE runs SET pmcid = :pmcid, updated_at = :ts WHERE id = :id"),
        {"pmcid": pmcid, "ts": now_iso(), "id": run_id},
    )


def update_run_status(
    conn: Connection, run_id: str, *, status: str, stage_reached: str | None = None
) -> None:
    conn.execute(
        text(
            "UPDATE runs SET status = :status, "
            "stage_reached = COALESCE(:stage_reached, stage_reached), "
            "updated_at = :ts WHERE id = :id"
        ),
        {"status": status, "stage_reached": stage_reached, "ts": now_iso(), "id": run_id},
    )


# Stage order, used by the UI's stage rail and by the CLI's `stage` command.
STAGE_ORDER = [
    "s0_acquire",
    "s1_explode",
    "s2_parse",
    "s3_classify",
    "s4_context",
    "s5_contract",
    "s6_project",
    "s7_normalize",
    "s8_validate",
    "s9_arbitrate",
    "s10_publish",
]


def stage_counts(conn: Connection, run_id: str) -> dict[str, dict[str, int]]:
    """Per-stage counts for the UI's stage rail, e.g.
    {"s3_classify": {"artifacts": 47, "assoc": 12, "needs_review": 3}}.
    Stages with no persisted output yet simply return an empty dict for that stage.
    """
    counts: dict[str, dict[str, int]] = {stage: {} for stage in STAGE_ORDER}

    top_level = conn.execute(
        text(
            "SELECT COUNT(*) FROM artifacts WHERE run_id = :id AND parent_artifact_id IS NULL"
        ),
        {"id": run_id},
    ).scalar_one()
    skipped = conn.execute(
        text(
            "SELECT COUNT(*) FROM artifacts WHERE run_id = :id AND parent_artifact_id IS NULL "
            "AND skip_reason IS NOT NULL"
        ),
        {"id": run_id},
    ).scalar_one()
    if top_level:
        counts["s0_acquire"]["found"] = top_level
        if skipped:
            counts["s0_acquire"]["skipped"] = skipped

    sheets = conn.execute(
        text(
            "SELECT COUNT(*) FROM artifacts WHERE run_id = :id AND parent_artifact_id IS NOT NULL"
        ),
        {"id": run_id},
    ).scalar_one()
    empty_sheets = conn.execute(
        text(
            "SELECT COUNT(*) FROM artifacts WHERE run_id = :id AND parent_artifact_id IS NOT NULL "
            "AND skip_reason = 'empty_sheet'"
        ),
        {"id": run_id},
    ).scalar_one()
    if sheets:
        counts["s1_explode"]["sheets"] = sheets
        if empty_sheets:
            counts["s1_explode"]["empty"] = empty_sheets

    parsed_artifacts = conn.execute(
        text("SELECT COUNT(DISTINCT artifact_id) FROM parsed_tables WHERE run_id = :id"),
        {"id": run_id},
    ).scalar_one()
    parsed_tables = conn.execute(
        text("SELECT COUNT(*) FROM parsed_tables WHERE run_id = :id"), {"id": run_id}
    ).scalar_one()
    if parsed_artifacts:
        counts["s2_parse"]["artifacts"] = parsed_artifacts
        counts["s2_parse"]["tables"] = parsed_tables

    classified = conn.execute(
        text("SELECT COUNT(*) FROM sheet_classifications WHERE run_id = :id"), {"id": run_id}
    ).scalar_one()
    classify_needs_review = conn.execute(
        text(
            "SELECT COUNT(*) FROM sheet_classifications WHERE run_id = :id "
            "AND needs_review = 1"
        ),
        {"id": run_id},
    ).scalar_one()
    if classified:
        counts["s3_classify"]["classified"] = classified
        if classify_needs_review:
            counts["s3_classify"]["needs_review"] = classify_needs_review

    methods_bundle_tokens = conn.execute(
        text(
            "SELECT token_count FROM methods_bundles WHERE run_id = :id "
            "ORDER BY created_at DESC LIMIT 1"
        ),
        {"id": run_id},
    ).scalar_one_or_none()
    if methods_bundle_tokens is not None:
        counts["s4_context"]["tokens"] = methods_bundle_tokens

    contracts = conn.execute(
        text(
            "SELECT COUNT(*) FROM schema_contracts sc "
            "JOIN parsed_tables pt ON pt.id = sc.parsed_table_id WHERE pt.run_id = :id"
        ),
        {"id": run_id},
    ).scalar_one()
    contracts_needs_review = conn.execute(
        text(
            "SELECT COUNT(*) FROM schema_contracts sc "
            "JOIN parsed_tables pt ON pt.id = sc.parsed_table_id "
            "WHERE pt.run_id = :id AND sc.needs_review = 1"
        ),
        {"id": run_id},
    ).scalar_one()
    if contracts:
        counts["s5_contract"]["contracts"] = contracts
        if contracts_needs_review:
            counts["s5_contract"]["needs_review"] = contracts_needs_review

    records = conn.execute(
        text("SELECT COUNT(*) FROM association_records WHERE run_id = :id"), {"id": run_id}
    ).scalar_one()
    if records:
        counts["s6_project"]["records"] = records

    # S7 mutates existing association_records rows rather than creating new
    # ones, so -- unlike every other stage here -- there's no row-existence
    # query that means "S7 ran." has_stage_finished is the marker instead.
    if events.has_stage_finished(conn, run_id, "s7_normalize"):
        genes_resolved = conn.execute(
            text(
                "SELECT COUNT(*) FROM association_records WHERE run_id = :id "
                "AND ensembl_gene_id IS NOT NULL"
            ),
            {"id": run_id},
        ).scalar_one()
        traits_resolved = conn.execute(
            text(
                "SELECT COUNT(*) FROM association_records WHERE run_id = :id "
                "AND efo_id IS NOT NULL"
            ),
            {"id": run_id},
        ).scalar_one()
        counts["s7_normalize"]["genes"] = genes_resolved
        counts["s7_normalize"]["traits"] = traits_resolved

    # S8 only ever inserts into check_results, but that table has no run_id
    # of its own (only a record_id -> association_records -> run_id join),
    # and a bare row-existence check can't distinguish "S8 ran" from "S8 ran
    # partially, then died" -- has_stage_finished is the same marker S7/S9
    # use for the same reason.
    if events.has_stage_finished(conn, run_id, "s8_validate"):
        v1_fail = conn.execute(
            text(
                "SELECT COUNT(*) FROM check_results cr "
                "JOIN association_records ar ON ar.record_id = cr.record_id "
                "WHERE ar.run_id = :id AND cr.check_name = 'v1_arithmetic' "
                "AND cr.status = 'fail'"
            ),
            {"id": run_id},
        ).scalar_one()
        v3_evaluated = conn.execute(
            text(
                "SELECT COUNT(DISTINCT cr.record_id) FROM check_results cr "
                "JOIN association_records ar ON ar.record_id = cr.record_id "
                "WHERE ar.run_id = :id AND cr.check_name LIKE 'v3_%'"
            ),
            {"id": run_id},
        ).scalar_one()
        counts["s8_validate"]["v1_fail"] = v1_fail
        counts["s8_validate"]["v3_evaluated"] = v3_evaluated

    # S9 hasn't run until it sets a *real* review_status -- S6 already
    # stamps every record "needs_review" as a provisional placeholder
    # that column directly would show S9 as "done" the moment S6 finishes,
    # before S9 exists. Gate on the same has_stage_finished marker as S7.
    if events.has_stage_finished(conn, run_id, "s9_arbitrate"):
        for status in ("auto_pass", "needs_review", "rejected"):
            n = conn.execute(
                text(
                    "SELECT COUNT(*) FROM association_records WHERE run_id = :id "
                    "AND review_status = :status"
                ),
                {"id": run_id, "status": status},
            ).scalar_one()
            if n:
                counts["s9_arbitrate"][status] = n

    # S10 writes files, not DB rows -- has_stage_finished plus the
    # stage_finished event's own payload (already computed once by S10
    # itself) is the only record of what it did, so read that back rather
    # than recomputing published/quarantined counts from scratch here.
    if events.has_stage_finished(conn, run_id, "s10_publish"):
        payload_json = conn.execute(
            text(
                "SELECT payload_json FROM events WHERE run_id = :id AND stage = 's10_publish' "
                "AND event_type = 'stage_finished' ORDER BY id DESC LIMIT 1"
            ),
            {"id": run_id},
        ).scalar_one_or_none()
        if payload_json:
            payload = json.loads(payload_json)
            if payload.get("records_published_main"):
                counts["s10_publish"]["published"] = payload["records_published_main"]
            if payload.get("records_published_quarantine"):
                counts["s10_publish"]["quarantined"] = payload["records_published_quarantine"]

    return counts
