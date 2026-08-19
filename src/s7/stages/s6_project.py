"""S6 - Projection (deterministic, no LLM). In: contract + full parsed table.
Out: raw AssociationRecord drafts, one per data row. Pure Python, no network
calls; must handle 100k rows in under 10 seconds.

Applies each table group's contract mechanically:
- one column_mapping's `transform` per source cell,
- `constant_fields` seeded first, per-row mappings override them,
- rows missing `trait_raw` (or both of gene_symbol_raw/variant_raw) are
  dropped as `incomplete_row`, counted, and reported -- never silently,
- effect_allele is never inferred: if it can't be
  resolved, effect_allele stays null and effect_direction is "unknown".

S6 owns the only INSERT into association_records; S7-S9 enrich the same
rows in place later (normalized IDs, check results, confidence).

One group commonly has two induced contracts (Anthropic + OpenAI, see
s5_contract.py). S6 must pick exactly one to project from, and there is no
inherently correct tiebreak, so `_select_primary_contract` is a documented
judgment call: prefer the one that isn't needs_review, then higher
confidence, then Anthropic on a dead tie. Both contracts stay visible in the
UI regardless of which one produced the records.
"""

from __future__ import annotations

import json
import math
import re
import typing
from typing import Any, get_args, get_origin

from sqlalchemy import Connection, Engine

from s7.models.record import AssociationRecord, record_id_for
from s7.models.stage import StageResult, StageStatus
from s7.store import events
from s7.store.artifacts import get_artifact
from s7.store.contracts import (
    list_column_mappings,
    list_contracts_for_run,
    list_member_table_ids,
)
from s7.store.db import get_engine, now_iso
from s7.store.parsed import get_parsed_table, list_parsed_cells
from s7.store.records import (
    delete_records_for_run,
    has_records_for_run,
    insert_association_records,
)
from s7.store.runs import get_run, update_run_status

STAGE = "s6_project"

NUMERIC_TARGET_FIELDS = {
    "effect_value",
    "standard_error",
    "p_value",
    "ci_lower",
    "ci_upper",
    "maf_threshold",
}
INTEGER_TARGET_FIELDS = {"n_total", "n_cases", "n_controls", "n_carriers"}
VALID_ENTITY_TYPES = {"gene", "variant"}

_LEADING_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*(?:[eE][-+]?\d+)?")


def _literal_choices(annotation: Any) -> frozenset[str]:
    """Flattens a field's Literal[...] | None annotation into its string
    values. Built from AssociationRecord itself (see below) rather than a
    hand-maintained set, so S6's enum validation can never drift from the
    model's own vocabulary.
    """
    origin = get_origin(annotation)
    if origin is typing.Literal:
        return frozenset(v for v in get_args(annotation) if isinstance(v, str))
    if origin is typing.Union or str(origin) == "types.UnionType":
        values: set[str] = set()
        for arg in get_args(annotation):
            values |= _literal_choices(arg)
        return frozenset(values)
    return frozenset()


# Every AssociationRecord field with a controlled vocabulary (trait_type,
# effect_type, effect_direction, test_method, variant_mask_class,
# analysis_role, ...) except entity_type, which keeps its own longstanding
# special case just below. A raw cell value that isn't in the field's
# vocabulary (after the alias table below) becomes None rather than passing
# through -- garbage in a Literal column would otherwise slip past S6
# entirely and crash S10's own validation on publish, which is exactly what
# happened against backman2021: raw "+"/"-" for effect_direction and
# "QT"/"BT" for trait_type were being written verbatim, failing
# AssociationRecord's schema for 2,087 of 2,406 records.
_ENUM_TARGET_FIELDS: dict[str, frozenset[str]] = {
    name: choices
    for name, field in AssociationRecord.model_fields.items()
    if name != "entity_type" and (choices := _literal_choices(field.annotation))
}

# Known real-world abbreviations seen in the corpus, lowercased. Anything
# not listed here and not already a valid value becomes None -- this table
# only ever narrows a known synonym to its canonical value, it never guesses.
_ENUM_ALIASES: dict[str, dict[str, str]] = {
    "trait_type": {"qt": "quantitative", "bt": "binary"},
    "effect_direction": {"+": "increases", "-": "decreases"},
}


def _coerce_numeric(raw: str) -> float | None:
    """Extracts a float from a cell that may not be a clean number --
    contracts routinely map a composite string (e.g. "0.116[0.099,0.132]")
    with transform=identity when the model didn't separate the components,
    so a bare float(raw) would crash the row instead of just that field.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        pass
    match = _LEADING_NUMBER_RE.search(raw)
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def _parse_ci_string(raw: str) -> tuple[float, float] | None:
    """Extracts (lower, upper) from a combined CI cell, e.g.
    "0.116[0.099,0.132]" or "(1.2, 3.4)" -- takes the *last* two numbers
    found so a leading point estimate before the bracket doesn't throw off
    which two numbers are the bounds.
    """
    numbers = _LEADING_NUMBER_RE.findall(raw)
    if len(numbers) < 2:
        return None
    try:
        a, b = float(numbers[-2].replace(",", "")), float(numbers[-1].replace(",", ""))
    except ValueError:
        return None
    return (a, b) if a <= b else (b, a)


def _coerce_field_value(target_field: str, raw: str) -> Any:
    raw = raw.strip()
    if not raw:
        return None
    if target_field in NUMERIC_TARGET_FIELDS:
        return _coerce_numeric(raw)
    if target_field in INTEGER_TARGET_FIELDS:
        n = _coerce_numeric(raw)
        return None if n is None else int(round(n))
    if target_field == "entity_type":
        normalized = raw.strip().lower()
        return normalized if normalized in VALID_ENTITY_TYPES else None
    if target_field in _ENUM_TARGET_FIELDS:
        normalized = raw.strip().lower()
        normalized = _ENUM_ALIASES.get(target_field, {}).get(normalized, normalized)
        return normalized if normalized in _ENUM_TARGET_FIELDS[target_field] else None
    return raw


def _apply_transform(transform: str | None, target_field: str, raw: str) -> Any:
    if transform == "parse_ci_string":
        bounds = _parse_ci_string(raw)
        if bounds is None:
            return None
        lower, upper = bounds
        return lower if target_field == "ci_lower" else upper
    n = _coerce_numeric(raw) if transform else None
    if transform == "neg_log10_to_p":
        return None if n is None else 10 ** (-n)
    if transform == "log_to_linear":
        return None if n is None else math.exp(n)
    if transform == "or_to_beta":
        return None if n is None or n <= 0 else math.log(n)
    if transform == "percent_to_fraction":
        return None if n is None else n / 100.0
    # identity, or no transform given
    return _coerce_field_value(target_field, raw)


def _resolve_column_index(header_rows: list[list[str | None]], name: str) -> int | None:
    """Finds `name` (an effect_allele_column value from the contract) among
    the header cells. Tries an exact match against any single header row
    first, then the per-column concatenation of all header rows -- multi-row
    merged headers mean the real column name can be the
    join of two rows, not any single cell's text.
    """
    if not header_rows or not name:
        return None
    name_norm = name.strip().lower()
    for row in header_rows:
        for i, cell in enumerate(row):
            if cell and str(cell).strip().lower() == name_norm:
                return i
    width = max((len(r) for r in header_rows), default=0)
    for i in range(width):
        parts = [str(row[i]) for row in header_rows if i < len(row) and row[i]]
        if " ".join(parts).strip().lower() == name_norm:
            return i
    return None


def _select_primary_contract(contracts: list[dict[str, Any]]) -> dict[str, Any]:
    def sort_key(c: dict[str, Any]) -> tuple[bool, float, int]:
        return (
            bool(c["needs_review"]),
            -float(c["overall_confidence"]),
            0 if str(c["model_spec"]).startswith("anthropic") else 1,
        )

    return sorted(contracts, key=sort_key)[0]


def _build_record_fields(
    *,
    cells_by_col: dict[int, str | None],
    mappings: list[dict[str, Any]],
    constant_fields: dict[str, Any],
    effect_allele_source: str,
    effect_allele_index: int | None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for name, raw_value in constant_fields.items():
        fields[name] = _coerce_field_value(name, str(raw_value))

    for m in mappings:
        target = m["target_field"]
        if not target:
            continue
        raw = cells_by_col.get(m["source_column_index"])
        if not raw:
            continue
        value = _apply_transform(m["transform"], target, raw)
        if value is not None:
            fields[target] = value  # per-row mapping overrides the table-level constant

    if effect_allele_source == "column" and effect_allele_index is not None:
        raw = cells_by_col.get(effect_allele_index)
        if raw:
            fields["effect_allele"] = raw.strip()
    # effect_allele_source == "constant": already seeded from constant_fields above.
    # effect_allele_source == "unresolvable": nothing to do.

    if not fields.get("effect_allele"):
        fields["effect_allele"] = None
        fields["effect_direction"] = "unknown"  # never inferred

    if fields.get("entity_type") not in VALID_ENTITY_TYPES:
        fields["entity_type"] = "variant" if fields.get("variant_raw") else "gene"

    return fields


def _is_complete(fields: dict[str, Any]) -> bool:
    if not fields.get("trait_raw"):
        return False
    return bool(fields.get("gene_symbol_raw") or fields.get("variant_raw"))


def _project_group(
    conn: Connection,
    *,
    run_row: dict[str, Any],
    contract: dict[str, Any],
    seen_record_ids: set[str],
    counts: dict[str, int],
) -> list[dict[str, Any]]:
    parsed_table_id = contract["parsed_table_id"]
    representative = get_parsed_table(conn, parsed_table_id)
    if representative is None:
        return []
    artifact = get_artifact(conn, representative["artifact_id"])
    if artifact is None:
        return []

    header_rows = json.loads(representative["header_rows_json"])
    effect_allele_index = (
        _resolve_column_index(header_rows, str(contract["effect_allele_column"]))
        if contract["effect_allele_source"] == "column" and contract["effect_allele_column"]
        else None
    )
    if contract["effect_allele_source"] == "column" and effect_allele_index is None:
        counts["effect_allele_column_unresolved"] += 1

    mappings = list_column_mappings(conn, contract["id"])
    constant_fields = json.loads(contract["constant_fields_json"])
    extracted_at = now_iso()

    records: list[dict[str, Any]] = []
    for table_id in list_member_table_ids(conn, contract["id"]):
        table = get_parsed_table(conn, table_id)
        if table is None:
            continue
        cells = list_parsed_cells(conn, table_id)

        by_row: dict[int, dict[int, str | None]] = {}
        page_by_row: dict[int, int | None] = {}
        for c in cells:
            by_row.setdefault(c["row_index"], {})[c["col_index"]] = c["value"]
            if c.get("page") is not None:
                page_by_row.setdefault(c["row_index"], c["page"])

        for row_index in sorted(by_row):
            fields = _build_record_fields(
                cells_by_col=by_row[row_index],
                mappings=mappings,
                constant_fields=constant_fields,
                effect_allele_source=contract["effect_allele_source"],
                effect_allele_index=effect_allele_index,
            )
            if not _is_complete(fields):
                counts["dropped_incomplete"] += 1
                continue

            entity_key = fields.get("gene_symbol_raw") or fields.get("variant_raw") or ""
            record_id = record_id_for(
                source_file_sha256=str(artifact["sha256"]),
                parsed_table_id=table_id,
                source_row_index=row_index,
                entity_key=str(entity_key),
                trait_raw=str(fields["trait_raw"]),
            )
            if record_id in seen_record_ids:
                counts["dropped_duplicate"] += 1
                continue
            seen_record_ids.add(record_id)

            records.append(
                {
                    "record_id": record_id,
                    "run_id": run_row["id"],
                    "pipeline_version": run_row["pipeline_version"],
                    "extracted_at": extracted_at,
                    "source_doi": run_row["doi"],
                    "source_pmcid": run_row["pmcid"],
                    "source_file_name": artifact["file_name"],
                    "source_file_sha256": artifact["sha256"],
                    "source_sheet_name": artifact["sheet_name"],
                    "source_row_index": row_index,
                    "source_page": page_by_row.get(row_index),
                    "source_parsed_table_id": table_id,
                    "extend_parse_run_id": table["extend_parse_run_id"],
                    "schema_contract_id": contract["id"],
                    # confidence/review_status are provisional -- S9 arbitration
                    # computes the real values; "needs_review"
                    # until then, never presented as auto_pass prematurely.
                    "confidence": contract["overall_confidence"],
                    "review_status": "needs_review",
                    **fields,
                }
            )
            counts["projected"] += 1

    return records


async def run(run_id: str, *, force: bool = False) -> StageResult:
    engine: Engine = get_engine()

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        already = has_records_for_run(conn, run_id)
        if not force and already:
            return StageResult(stage=STAGE, status="skipped", counts={})
        if force and already:
            delete_records_for_run(conn, run_id)

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S6 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)

        all_contracts = list_contracts_for_run(conn, run_id)
        groups: dict[str, list[dict[str, Any]]] = {}
        for c in all_contracts:
            groups.setdefault(c["agreement_group_id"], []).append(c)

        counts: dict[str, int] = {
            "groups_processed": 0,
            "projected": 0,
            "dropped_incomplete": 0,
            "dropped_duplicate": 0,
            "effect_allele_column_unresolved": 0,
        }
        seen_record_ids: set[str] = set()

        for group_contracts in groups.values():
            primary = _select_primary_contract(group_contracts)
            records = _project_group(
                conn,
                run_row=run_row,
                contract=primary,
                seen_record_ids=seen_record_ids,
                counts=counts,
            )
            insert_association_records(conn, records)
            counts["groups_processed"] += 1

        status: StageStatus = "done"
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S6 projection finished: {counts['projected']} records from "
            f"{counts['groups_processed']} groups "
            f"({counts['dropped_incomplete']} dropped incomplete, "
            f"{counts['dropped_duplicate']} dropped duplicate)",
            payload=counts,
        )

    return StageResult(stage=STAGE, status=status, counts=counts)
