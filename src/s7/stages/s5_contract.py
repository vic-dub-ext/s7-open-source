"""S5 - Schema contract induction (LLM). In: one parsed table's header block +
first 20 data rows + the MethodsBundle. Out: one SchemaContract, induced twice
(Anthropic + OpenAI) so disagreement is visible.

Only tables from artifacts classified as one of the assoc_* categories carry
association results worth a contract -- decoder tables (mask/phenotype
definitions) feed S4's context instead, and cohort/QC/other tables are never
projected into AssociationRecords.

Extend's block/chunk parsing commonly splits one logical table into many
small `parsed_tables` rows sharing a single header (a 40-column, 700-row
supplementary table can arrive as 140+ fragments, some carrying no header of
their own). Inducing a contract per raw fragment would be both wasteful and
mostly duplicate, so tables are first coalesced within each artifact by
header signature (see `_group_tables_by_header`) -- one contract per group,
covering every fragment in `contract_table_members`.

Each group gets induced independently by both providers, then the two
inductions are compared: any disagreement on a column's target_field or on
effect_allele_source marks both contracts needs_review and is logged as a
contract_disagreement event: agreement is cheap evidence, and disagreement
is exactly what's worth looking at.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from sqlalchemy import Engine

from s7.config import ModelSpec, Settings, get_settings
from s7.models.contract import ContractInduction
from s7.models.stage import StageResult, StageStatus
from s7.prompts import render_prompt
from s7.providers.llm import LLMCallRecord, LLMError, complete_json
from s7.store import events
from s7.store.classifications import list_by_effective_classification
from s7.store.context import get_methods_bundle_for_run
from s7.store.contracts import (
    delete_contracts_for_run,
    has_contracts_for_run,
    insert_column_mappings,
    insert_contract_table_members,
    insert_schema_contract,
)
from s7.store.db import get_engine, new_id
from s7.store.llm_calls import delete_llm_calls_for_run, insert_llm_call
from s7.store.parsed import list_parsed_cells, list_parsed_tables_for_artifact
from s7.store.runs import get_run, update_run_status

STAGE = "s5_contract"
MAX_CONCURRENCY = 3
MAX_SAMPLE_ROWS = 20

ASSOC_CLASSIFICATIONS = [
    "assoc_gene_level",
    "assoc_variant_level",
    "assoc_conditional",
    "assoc_replication",
]


def _group_tables_by_header(tables: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Coalesce parsed_table fragments that share one logical table.

    `tables` must already be in document order (list_parsed_tables_for_artifact
    orders by created_at). A fragment with a real header starts a new group
    unless its header is byte-identical to the group currently open (a
    repeated per-block header restating the same columns, not a new table).
    A fragment with no header at all (its header line was chunked away)
    joins whatever group is currently open -- or, if none is open yet,
    starts a headerless group of its own, since there's no earlier header
    to recover it into.
    """
    groups: list[list[dict[str, Any]]] = []
    current_header_key: str | None = None
    for table in tables:
        header_rows = json.loads(table["header_rows_json"])
        header_key = json.dumps(header_rows) if header_rows else None
        is_new_header = header_key is not None and header_key != current_header_key
        starts_new_group = not groups or is_new_header
        if starts_new_group:
            groups.append([table])
            if header_key is not None:
                current_header_key = header_key
        else:
            groups[-1].append(table)
    return groups


def _render_header_block(header_rows: list[Any], width: int) -> str:
    if not header_rows:
        return "(no header captured)"
    lines = []
    for row in header_rows:
        padded = list(row) + [None] * (width - len(row))
        lines.append(" | ".join(str(c) if c is not None else "" for c in padded))
    return "\n".join(lines)


def _sample_group_rows(
    engine: Engine, group: list[dict[str, Any]], width: int, max_rows: int
) -> tuple[str, int]:
    """Up to `max_rows` data rows sampled across the group's fragments in
    order, stopping as soon as enough are collected -- a 700-row group
    should not require loading all 700 rows just to render 20 of them.
    """
    lines: list[str] = []
    for table in group:
        if len(lines) >= max_rows:
            break
        with engine.begin() as conn:
            cells = list_parsed_cells(conn, table["id"])
        by_row: dict[int, dict[int, str]] = {}
        for c in cells:
            by_row.setdefault(c["row_index"], {})[c["col_index"]] = c["value"] or ""
        for row_index in sorted(by_row):
            if len(lines) >= max_rows:
                break
            row = by_row[row_index]
            lines.append(" | ".join(row.get(i, "") for i in range(width)))
    return ("\n".join(lines) if lines else "(no data rows captured)"), len(lines)


def _mappings_disagree(a: ContractInduction, b: ContractInduction) -> bool:
    """True if the two inductions assign a different target_field to any
    source column, or disagree on effect_allele_source. Wording differences
    in constant_fields/unmapped_columns don't count.
    """
    if a.effect_allele_source != b.effect_allele_source:
        return True

    def targets_by_index(induction: ContractInduction) -> dict[int, frozenset[str | None]]:
        # A single column can carry multiple target fields (e.g. a combined
        # "Effect (95% CI)" column splits into effect_value + ci_lower +
        # ci_upper), so compare the *set* of targets per column, not just
        # the last one seen -- a plain dict comprehension would silently
        # drop all but one mapping per index.
        by_index: dict[int, set[str | None]] = {}
        for m in induction.column_mappings:
            by_index.setdefault(m.source_column_index, set()).add(m.target_field)
        return {i: frozenset(targets) for i, targets in by_index.items()}

    by_index_a = targets_by_index(a)
    by_index_b = targets_by_index(b)
    all_indices = set(by_index_a) | set(by_index_b)
    empty: frozenset[str | None] = frozenset()
    return any(by_index_a.get(i, empty) != by_index_b.get(i, empty) for i in all_indices)


async def _induce_one(
    *, settings: Settings, model: ModelSpec, system: str, user: str, entity_id: str
) -> tuple[ContractInduction | None, LLMCallRecord]:
    """Wraps complete_json so a failure returns (None, failed_record) instead
    of propagating -- one provider failing shouldn't abort the other.
    """
    try:
        parsed, record = await complete_json(
            settings=settings,
            model=model,
            system=system,
            user=user,
            response_model=ContractInduction,
            stage=STAGE,
            entity_id=entity_id,
        )
        return parsed, record
    except LLMError as exc:
        return None, exc.record


async def _induce_group(
    engine: Engine,
    settings: Settings,
    *,
    run_id: str,
    group: list[dict[str, Any]],
    methods_bundle: str,
    counts: dict[str, int],
    counts_lock: asyncio.Lock,
) -> None:
    representative = group[0]
    representative_id = representative["id"]
    header_rows = json.loads(representative["header_rows_json"])
    total_rows = sum(int(t["row_count"]) for t in group)

    if not header_rows and total_rows == 0:
        async with counts_lock:
            counts["skipped"] += 1
        return

    width = max((int(t["col_count"]) for t in group), default=0) or max(
        (len(r) for r in header_rows), default=0
    )
    header_block = _render_header_block(header_rows, width)
    data_rows_block, sampled_rows = _sample_group_rows(engine, group, width, MAX_SAMPLE_ROWS)

    system, user = render_prompt(
        "s5_contract_induction",
        header_block=header_block,
        data_rows_block=data_rows_block,
        row_count=sampled_rows,
        methods_bundle=methods_bundle or "(no methods text available for this paper)",
    )

    anthropic_result, anthropic_record = await _induce_one(
        settings=settings,
        model=settings.anthropic_spec,
        system=system,
        user=user,
        entity_id=representative_id,
    )
    openai_result, openai_record = await _induce_one(
        settings=settings,
        model=settings.openai_spec,
        system=system,
        user=user,
        entity_id=representative_id,
    )

    with engine.begin() as conn:
        for record in (anthropic_record, openai_record):
            insert_llm_call(
                conn,
                run_id=run_id,
                stage=STAGE,
                entity_id=record.entity_id,
                provider=record.model_spec.provider,
                model=record.model_spec.model,
                prompt_hash=record.prompt_hash,
                prompt=record.prompt,
                response=record.response,
                input_tokens=record.input_tokens,
                output_tokens=record.output_tokens,
                cost_usd=record.cost_usd,
                latency_ms=record.latency_ms,
                ok=record.ok,
            )

        results = [
            (settings.anthropic_spec, anthropic_result),
            (settings.openai_spec, openai_result),
        ]
        succeeded = [(model, result) for model, result in results if result is not None]

        if not succeeded:
            events.emit(
                conn,
                run_id=run_id,
                stage=STAGE,
                entity_id=representative_id,
                event_type="error",
                level="error",
                message=f"contract induction failed for table {representative_id}: "
                "both providers errored",
            )
            counts_delta = {"failed": 1}
        else:
            disagree = len(succeeded) == 2 and _mappings_disagree(succeeded[0][1], succeeded[1][1])
            needs_review = disagree or len(succeeded) == 1
            agreement_group_id = new_id()
            contract_ids = []
            for model, induction in succeeded:
                contract_id = insert_schema_contract(
                    conn,
                    parsed_table_id=representative_id,
                    model_spec=f"{model.provider}:{model.model}",
                    row_entity=induction.row_entity,
                    constant_fields={cf.field: cf.value for cf in induction.constant_fields},
                    effect_allele_source=induction.effect_allele_source,
                    effect_allele_column=induction.effect_allele_column,
                    unmapped_columns=induction.unmapped_columns,
                    interpretation_notes=induction.interpretation_notes,
                    overall_confidence=induction.overall_confidence,
                    needs_review=needs_review,
                    agreement_group_id=agreement_group_id,
                )
                insert_column_mappings(
                    conn, contract_id, [m.model_dump() for m in induction.column_mappings]
                )
                insert_contract_table_members(conn, contract_id, [t["id"] for t in group])
                contract_ids.append(contract_id)

            events.emit(
                conn,
                run_id=run_id,
                stage=STAGE,
                entity_id=representative_id,
                event_type="contract_induced",
                message=f"table {representative_id} ({len(group)} fragments, {total_rows} rows): "
                f"{len(succeeded)}/2 providers succeeded" + (", DISAGREEMENT" if disagree else ""),
                payload={
                    "contract_ids": contract_ids,
                    "agreement_group_id": agreement_group_id,
                    "needs_review": needs_review,
                    "providers_succeeded": [m.provider for m, _ in succeeded],
                    "fragment_count": len(group),
                    "total_rows": total_rows,
                },
            )
            if disagree:
                events.emit(
                    conn,
                    run_id=run_id,
                    stage=STAGE,
                    entity_id=representative_id,
                    event_type="contract_disagreement",
                    level="warn",
                    message=f"table {representative_id}: providers disagree on column mapping "
                    "or effect_allele_source",
                    payload={"contract_ids": contract_ids},
                )
            counts_delta = {
                "contracts_induced": len(succeeded),
                "needs_review": 1 if needs_review else 0,
                "disagreements": 1 if disagree else 0,
                "partial": 1 if len(succeeded) == 1 else 0,
            }

    async with counts_lock:
        for key, delta in counts_delta.items():
            counts[key] = counts.get(key, 0) + delta
        counts["groups_processed"] += 1
        counts["fragments_covered"] += len(group)


async def run(run_id: str, *, force: bool = False) -> StageResult:
    settings = get_settings()
    settings.ensure_dirs()
    engine = get_engine()

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        already = has_contracts_for_run(conn, run_id)
        if not force and already:
            return StageResult(stage=STAGE, status="skipped", counts={})
        if force and already:
            delete_contracts_for_run(conn, run_id)
            delete_llm_calls_for_run(conn, run_id, stage=STAGE)

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S5 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)

        assoc_artifacts = list_by_effective_classification(conn, run_id, ASSOC_CLASSIFICATIONS)
        bundle = get_methods_bundle_for_run(conn, run_id)
        methods_bundle = str(bundle["content"]) if bundle else ""

        groups: list[list[dict[str, Any]]] = []
        for artifact in assoc_artifacts:
            artifact_tables = list_parsed_tables_for_artifact(conn, artifact["artifact_id"])
            groups.extend(_group_tables_by_header(artifact_tables))

    counts: dict[str, int] = {
        "groups_processed": 0,
        "fragments_covered": 0,
        "contracts_induced": 0,
        "needs_review": 0,
        "disagreements": 0,
        "partial": 0,
        "failed": 0,
        "skipped": 0,
    }
    counts_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def bounded(group: list[dict[str, Any]]) -> None:
        async with semaphore:
            await _induce_group(
                engine,
                settings,
                run_id=run_id,
                group=group,
                methods_bundle=methods_bundle,
                counts=counts,
                counts_lock=counts_lock,
            )

    if groups:
        await asyncio.gather(*(bounded(g) for g in groups))

    status: StageStatus = "done" if counts["failed"] == 0 else "partial"
    with engine.begin() as conn:
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S5 contract induction finished: {counts['contracts_induced']} contracts "
            f"from {counts['groups_processed']} groups covering {counts['fragments_covered']} "
            f"fragments ({counts['needs_review']} needs_review, {counts['disagreements']} "
            f"disagreements), {counts['failed']} failed, {counts['skipped']} skipped",
            payload=counts,
        )

    return StageResult(stage=STAGE, status=status, counts=counts)
