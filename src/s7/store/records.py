"""CRUD for S6's output: `association_records`. S7-S9 enrich these same rows
in place (normalized IDs, check results, confidence, review_status) rather
than reinserting -- S6 owns the only INSERT.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import Connection, text

_COLUMNS = [
    "record_id",
    "run_id",
    "pipeline_version",
    "extracted_at",
    "source_doi",
    "source_pmcid",
    "source_file_name",
    "source_file_sha256",
    "source_sheet_name",
    "source_row_index",
    "source_page",
    "source_parsed_table_id",
    "extend_parse_run_id",
    "schema_contract_id",
    "entity_type",
    "gene_symbol_raw",
    "variant_raw",
    "rsid",
    "trait_raw",
    "trait_label",
    "trait_type",
    "trait_units",
    "test_method_raw",
    "variant_mask_raw",
    "maf_threshold",
    "effect_value",
    "effect_type",
    "effect_allele",
    "other_allele",
    "effect_direction",
    "standard_error",
    "p_value",
    "ci_lower",
    "ci_upper",
    "cohort_name",
    "ancestry",
    "n_total",
    "n_cases",
    "n_controls",
    "n_carriers",
    "analysis_role",
    "confidence",
    "review_status",
]


def insert_association_records(conn: Connection, records: list[dict[str, Any]]) -> None:
    """Bulk insert. `records` are plain dicts keyed by column name -- missing
    keys are inserted as NULL. Must stay a single executemany-style call:
    S6 has to handle 100k rows in under 10 seconds, so no per-row round trip.
    """
    if not records:
        return
    placeholders = ", ".join(f":{c}" for c in _COLUMNS)
    rows = [{c: r.get(c) for c in _COLUMNS} for r in records]
    conn.execute(
        text(
            f"INSERT INTO association_records ({', '.join(_COLUMNS)}) VALUES ({placeholders})"
        ),
        rows,
    )


def has_records_for_run(conn: Connection, run_id: str) -> bool:
    count = conn.execute(
        text("SELECT COUNT(*) FROM association_records WHERE run_id = :run_id"),
        {"run_id": run_id},
    ).scalar_one()
    return bool(count)


def delete_records_for_run(conn: Connection, run_id: str) -> None:
    conn.execute(
        text(
            "DELETE FROM check_results WHERE record_id IN "
            "(SELECT record_id FROM association_records WHERE run_id = :run_id)"
        ),
        {"run_id": run_id},
    )
    conn.execute(
        text("DELETE FROM association_records WHERE run_id = :run_id"), {"run_id": run_id}
    )


def update_normalized_fields(conn: Connection, updates: list[dict[str, Any]]) -> None:
    """S7's write-back: normalized gene/variant/trait identifiers onto
    already-inserted rows (S6 owns the INSERT; S7-S9 only ever UPDATE). Every
    field defaults to None for a record whose value didn't resolve -- safe,
    because S6 never populates these columns itself.
    """
    if not updates:
        return
    columns = ["ensembl_gene_id", "chrom", "pos_b38", "ref", "alt", "rsid", "efo_id"]
    rows = [
        {
            "record_id": u["record_id"],
            **{c: u.get(c) for c in columns},
            "strand_ambiguous": int(u.get("strand_ambiguous", False)),
        }
        for u in updates
    ]
    set_clause = ", ".join(f"{c} = :{c}" for c in [*columns, "strand_ambiguous"])
    conn.execute(
        text(f"UPDATE association_records SET {set_clause} WHERE record_id = :record_id"),
        rows,
    )


def update_arbitration(conn: Connection, updates: list[dict[str, Any]]) -> None:
    """S9's write-back: `confidence`/`review_status`, replacing S6's
    provisional placeholder (see s6_project.py). Each dict needs record_id,
    confidence, review_status.
    """
    if not updates:
        return
    conn.execute(
        text(
            "UPDATE association_records SET confidence = :confidence, "
            "review_status = :review_status WHERE record_id = :record_id"
        ),
        updates,
    )


def list_record_identity_fields(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    """record_id + the three raw fields S7 normalizes from -- not the full
    row, to keep this cheap at 100k-row scale.
    """
    rows = conn.execute(
        text(
            "SELECT record_id, gene_symbol_raw, variant_raw, trait_raw "
            "FROM association_records WHERE run_id = :run_id"
        ),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]


def list_records_for_run(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    """Full rows -- S8's three checks each need most of a record's fields."""
    rows = conn.execute(
        text("SELECT * FROM association_records WHERE run_id = :run_id"),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]


def count_records_for_run(conn: Connection, run_id: str) -> int:
    return int(
        conn.execute(
            text("SELECT COUNT(*) FROM association_records WHERE run_id = :run_id"),
            {"run_id": run_id},
        ).scalar_one()
    )


def count_needs_review_for_run(conn: Connection, run_id: str) -> int:
    """0 before S9 runs -- review_status is still S6's provisional placeholder
    until then (see s6_project.py), which would overcount if this were read
    without gating on S9 having actually run. Callers that need that
    distinction should check events.has_stage_finished themselves; this is
    just the raw count for wherever that's already been established.
    """
    return int(
        conn.execute(
            text(
                "SELECT COUNT(*) FROM association_records "
                "WHERE run_id = :run_id AND review_status = 'needs_review'"
            ),
            {"run_id": run_id},
        ).scalar_one()
    )
