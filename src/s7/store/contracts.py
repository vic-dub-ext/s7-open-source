"""CRUD for S5's output: `schema_contracts` and `column_mappings`."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from s7.store.db import new_id, now_iso


def insert_schema_contract(
    conn: Connection,
    *,
    parsed_table_id: str,
    model_spec: str,
    row_entity: str,
    constant_fields: dict[str, Any],
    effect_allele_source: str,
    effect_allele_column: str | None,
    unmapped_columns: list[str],
    interpretation_notes: str,
    overall_confidence: float,
    needs_review: bool,
    agreement_group_id: str,
) -> str:
    contract_id = new_id()
    conn.execute(
        text(
            "INSERT INTO schema_contracts (id, parsed_table_id, model_spec, row_entity, "
            "constant_fields_json, effect_allele_source, effect_allele_column, "
            "unmapped_columns_json, interpretation_notes, overall_confidence, needs_review, "
            "agreement_group_id, created_at) VALUES (:id, :parsed_table_id, :model_spec, "
            ":row_entity, :constant_fields_json, :effect_allele_source, :effect_allele_column, "
            ":unmapped_columns_json, :interpretation_notes, :overall_confidence, :needs_review, "
            ":agreement_group_id, :created_at)"
        ),
        {
            "id": contract_id,
            "parsed_table_id": parsed_table_id,
            "model_spec": model_spec,
            "row_entity": row_entity,
            "constant_fields_json": json.dumps(constant_fields),
            "effect_allele_source": effect_allele_source,
            "effect_allele_column": effect_allele_column,
            "unmapped_columns_json": json.dumps(unmapped_columns),
            "interpretation_notes": interpretation_notes,
            "overall_confidence": overall_confidence,
            "needs_review": int(needs_review),
            "agreement_group_id": agreement_group_id,
            "created_at": now_iso(),
        },
    )
    return contract_id


def insert_column_mappings(
    conn: Connection, contract_id: str, mappings: list[dict[str, Any]]
) -> None:
    if not mappings:
        return
    rows = [
        {
            "id": new_id(),
            "contract_id": contract_id,
            "source_column": m["source_column"],
            "source_column_index": m["source_column_index"],
            "target_field": m.get("target_field"),
            "transform": m.get("transform"),
            "unit": m.get("unit"),
            "evidence": m["evidence"],
            "confidence": m["confidence"],
        }
        for m in mappings
    ]
    conn.execute(
        text(
            "INSERT INTO column_mappings (id, contract_id, source_column, source_column_index, "
            "target_field, transform, unit, evidence, confidence) VALUES "
            "(:id, :contract_id, :source_column, :source_column_index, :target_field, "
            ":transform, :unit, :evidence, :confidence)"
        ),
        rows,
    )


def insert_contract_table_members(
    conn: Connection, contract_id: str, parsed_table_ids: list[str]
) -> None:
    """Records every parsed_table fragment one contract covers -- see
    schema.sql's comment on contract_table_members for why a contract's own
    parsed_table_id (the representative fragment) usually isn't the only one.
    """
    if not parsed_table_ids:
        return
    conn.execute(
        text(
            "INSERT INTO contract_table_members (contract_id, parsed_table_id) "
            "VALUES (:contract_id, :parsed_table_id)"
        ),
        [{"contract_id": contract_id, "parsed_table_id": pid} for pid in parsed_table_ids],
    )


def list_member_table_ids(conn: Connection, contract_id: str) -> list[str]:
    rows = conn.execute(
        text("SELECT parsed_table_id FROM contract_table_members WHERE contract_id = :id"),
        {"id": contract_id},
    )
    return [str(r[0]) for r in rows]


def has_contracts_for_run(conn: Connection, run_id: str) -> bool:
    count = conn.execute(
        text(
            "SELECT COUNT(*) FROM schema_contracts sc "
            "JOIN parsed_tables pt ON pt.id = sc.parsed_table_id "
            "WHERE pt.run_id = :run_id"
        ),
        {"run_id": run_id},
    ).scalar_one()
    return bool(count)


def delete_contracts_for_run(conn: Connection, run_id: str) -> None:
    matching_contracts = (
        "(SELECT sc.id FROM schema_contracts sc "
        "JOIN parsed_tables pt ON pt.id = sc.parsed_table_id WHERE pt.run_id = :run_id)"
    )
    conn.execute(
        text(f"DELETE FROM contract_table_members WHERE contract_id IN {matching_contracts}"),
        {"run_id": run_id},
    )
    conn.execute(
        text(f"DELETE FROM column_mappings WHERE contract_id IN {matching_contracts}"),
        {"run_id": run_id},
    )
    conn.execute(
        text(f"DELETE FROM schema_contracts WHERE id IN {matching_contracts}"),
        {"run_id": run_id},
    )


def list_contracts_for_table(conn: Connection, parsed_table_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT * FROM schema_contracts WHERE parsed_table_id = :id ORDER BY created_at"),
        {"id": parsed_table_id},
    )
    return [dict(r._mapping) for r in rows]


def get_schema_contract(conn: Connection, contract_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT * FROM schema_contracts WHERE id = :id"), {"id": contract_id}
    ).first()
    return dict(row._mapping) if row else None


def list_contracts_for_agreement_group(
    conn: Connection, agreement_group_id: str
) -> list[dict[str, Any]]:
    """The 1-2 contracts induced for one table group -- the contract
    inspector's side-by-side dual-model diff view. Joined with the table's
    artifact for display context.
    """
    rows = conn.execute(
        text(
            "SELECT sc.*, pt.artifact_id, pt.header_rows_json, pt.col_count, "
            "a.file_name, a.sheet_name "
            "FROM schema_contracts sc "
            "JOIN parsed_tables pt ON pt.id = sc.parsed_table_id "
            "JOIN artifacts a ON a.id = pt.artifact_id "
            "WHERE sc.agreement_group_id = :agreement_group_id ORDER BY sc.model_spec"
        ),
        {"agreement_group_id": agreement_group_id},
    )
    return [dict(r._mapping) for r in rows]


def list_column_mappings(conn: Connection, contract_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT * FROM column_mappings WHERE contract_id = :id ORDER BY source_column_index"),
        {"id": contract_id},
    )
    return [dict(r._mapping) for r in rows]


def list_contracts_for_run(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    """Joined with the table's artifact -- the contract inspector's entry list."""
    rows = conn.execute(
        text(
            "SELECT sc.*, pt.artifact_id, a.file_name, a.sheet_name "
            "FROM schema_contracts sc "
            "JOIN parsed_tables pt ON pt.id = sc.parsed_table_id "
            "JOIN artifacts a ON a.id = pt.artifact_id "
            "WHERE pt.run_id = :run_id ORDER BY sc.created_at"
        ),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]
