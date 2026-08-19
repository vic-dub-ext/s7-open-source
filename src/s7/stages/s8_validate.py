"""S8 - Validation. Three checks per record.

V1 -- Arithmetic consistency. Pure code, no LLM, every record: recomputed
p-value vs. reported (log scale), CI/SE/effect consistency, and range
checks (0<p<=1, SE>0, odds ratios>0, case/control/carrier counts, MAF).

V2 -- Grounding. Code only (no LLM adjudication step yet -- a documented
simplification, see below). For a sample of each contract's records (all of
them if under 500, otherwise 500), re-reads the exact source cell at
the record's stored (parsed_table, row_index) and compares it to the
record's stored value for each *identity* field (gene_symbol_raw,
variant_raw, trait_raw) that came from an identity-transform column
mapping -- these are the fields a header-detection row-shift would
misalign first and most visibly. A mismatch rate above 1% fails the WHOLE
contract, not just the sampled rows -- at that rate the cause is almost
always a row-shift from a mis-detected header block.

V3 -- Semantic. LLM, two model families, verdicts recorded separately, on
a stratified sample per contract plus every record V1 or V2 failed.

S8 only ever inserts into check_results; it never touches
association_records itself -- S9 arbitration is what reads these results
back into confidence/review_status.
"""

from __future__ import annotations

import asyncio
import math
import random
from typing import Any

from s7.config import ModelSpec, Settings, get_settings
from s7.models.stage import StageResult, StageStatus
from s7.models.validation import V3SemanticVerdict
from s7.prompts import render_prompt
from s7.providers.llm import LLMCallRecord, LLMError, complete_json
from s7.store import events
from s7.store.checks import delete_checks_for_run, has_checks_for_run, insert_check_results
from s7.store.context import get_methods_bundle_for_run
from s7.store.contracts import get_schema_contract, list_column_mappings
from s7.store.db import get_engine
from s7.store.llm_calls import delete_llm_calls_for_run, insert_llm_call
from s7.store.parsed import get_cells_for_row
from s7.store.records import list_records_for_run
from s7.store.runs import get_run, update_run_status

STAGE = "s8_validate"

V1_LOG_P_TOLERANCE = 0.5
V1_CI_SE_RELATIVE_TOLERANCE = 0.25

V2_SAMPLE_SIZE = 500
V2_MISMATCH_THRESHOLD = 0.01
IDENTITY_TARGET_FIELDS = ("gene_symbol_raw", "variant_raw", "trait_raw")

V3_SAMPLE_PER_CONTRACT = 5
V3_CONCURRENCY = 5


def _v1_arithmetic(record: dict[str, Any]) -> dict[str, str]:
    """Pure code. Returns a CheckResult-shaped dict
    (record_id filled in by the caller).
    """
    failures: list[str] = []
    checked_anything = False

    effect = record.get("effect_value")
    se = record.get("standard_error")
    p = record.get("p_value")
    if effect is not None and se is not None and se > 0 and p is not None and p > 0:
        checked_anything = True
        z = abs(effect / se)
        p_computed = math.erfc(z / math.sqrt(2))
        if p_computed > 0:
            log_diff = abs(math.log10(p) - math.log10(p_computed))
            if log_diff > V1_LOG_P_TOLERANCE:
                failures.append(
                    f"reported p={p:.3g} vs. recomputed p={p_computed:.3g} from "
                    f"effect/SE (log10 diff {log_diff:.2f} > {V1_LOG_P_TOLERANCE})"
                )

    ci_lower, ci_upper = record.get("ci_lower"), record.get("ci_upper")
    if effect is not None and ci_lower is not None and ci_upper is not None:
        checked_anything = True
        lo, hi = min(ci_lower, ci_upper), max(ci_lower, ci_upper)
        if not (lo - 1e-9 <= effect <= hi + 1e-9):
            failures.append(f"effect {effect:.4g} outside reported CI [{lo:.4g}, {hi:.4g}]")
        if se is not None and se > 0:
            implied_se = (hi - lo) / (2 * 1.96)
            if abs(implied_se - se) / se > V1_CI_SE_RELATIVE_TOLERANCE:
                failures.append(
                    f"CI-implied SE {implied_se:.3g} inconsistent with reported SE {se:.3g}"
                )

    if p is not None:
        checked_anything = True
        if not (0 < p <= 1):
            failures.append(f"p_value {p} outside (0, 1]")

    if se is not None:
        checked_anything = True
        if se <= 0:
            failures.append(f"standard_error {se} <= 0")

    if record.get("effect_type") == "odds_ratio" and effect is not None:
        checked_anything = True
        if effect <= 0:
            failures.append(f"odds_ratio {effect} <= 0")

    n_total, n_cases, n_controls = (
        record.get("n_total"),
        record.get("n_cases"),
        record.get("n_controls"),
    )
    if n_total is not None and n_cases is not None and n_controls is not None:
        checked_anything = True
        if n_cases + n_controls > n_total:
            failures.append(f"n_cases+n_controls ({n_cases + n_controls}) > n_total ({n_total})")

    n_carriers = record.get("n_carriers")
    if n_carriers is not None and n_total is not None:
        checked_anything = True
        if n_carriers > n_total:
            failures.append(f"n_carriers ({n_carriers}) > n_total ({n_total})")

    maf = record.get("maf_threshold")
    if maf is not None:
        checked_anything = True
        if not (0 <= maf <= 1):
            failures.append(f"maf_threshold {maf} outside [0, 1]")

    if not checked_anything:
        return {"status": "skip", "detail": "no applicable numeric fields present"}
    if failures:
        return {"status": "fail", "detail": "; ".join(failures)}
    return {"status": "pass", "detail": "all applicable arithmetic checks passed"}


def _identity_field_mismatch(
    record: dict[str, Any], mappings_by_field: dict[str, dict[str, Any]], conn: Any
) -> bool | None:
    """None if nothing about this record could be checked (no identity
    field backed by an identity-transform column mapping, or no source
    coordinates recorded); otherwise True/False for mismatch/match.
    """
    table_id = record.get("source_parsed_table_id")
    if table_id is None:
        return None
    cells = get_cells_for_row(conn, table_id, record["source_row_index"])
    checked = False
    for field in IDENTITY_TARGET_FIELDS:
        mapping = mappings_by_field.get(field)
        stored = record.get(field)
        if mapping is None or stored is None:
            continue
        if mapping.get("transform") not in (None, "identity"):
            continue
        raw_cell = cells.get(mapping["source_column_index"])
        checked = True
        if raw_cell is None or str(raw_cell).strip() != str(stored).strip():
            return True
    return False if checked else None


def _render_row_block(record: dict[str, Any]) -> str:
    fields = [
        "entity_type",
        "gene_symbol_raw",
        "variant_raw",
        "trait_raw",
        "trait_type",
        "test_method_raw",
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
        "analysis_role",
    ]
    lines = [f"{f}: {record[f]}" for f in fields if record.get(f) is not None]
    return "\n".join(lines)


async def _run_v3(
    *, settings: Settings, model: ModelSpec, system: str, user: str, record_id: str
) -> tuple[V3SemanticVerdict | None, LLMCallRecord]:
    try:
        parsed, record = await complete_json(
            settings=settings,
            model=model,
            system=system,
            user=user,
            response_model=V3SemanticVerdict,
            stage=STAGE,
            entity_id=record_id,
        )
        return parsed, record
    except LLMError as exc:
        return None, exc.record


def _v3_check_results(
    record_id: str, model: ModelSpec, verdict: V3SemanticVerdict
) -> list[dict[str, Any]]:
    checked_by = f"llm:{model.provider}:{model.model}"
    pairs = [
        (
            "v3_semantic_effect_allele",
            verdict.effect_allele_correct,
            verdict.effect_allele_reasoning,
        ),
        (
            "v3_semantic_effect_direction",
            verdict.effect_direction_consistent,
            verdict.effect_direction_reasoning,
        ),
        (
            "v3_semantic_trait_type",
            verdict.trait_type_and_effect_type_appropriate,
            verdict.trait_type_reasoning,
        ),
        (
            "v3_semantic_analysis_role",
            verdict.analysis_role_correct,
            verdict.analysis_role_reasoning,
        ),
    ]
    return [
        {
            "record_id": record_id,
            "check_name": name,
            "status": "pass" if ok else "fail",
            "detail": reasoning,
            "checked_by": checked_by,
        }
        for name, ok, reasoning in pairs
    ]


async def run(
    run_id: str,
    *,
    force: bool = False,
    v3_models: tuple[ModelSpec, ...] | None = None,
    v3_max_records: int | None = None,
) -> StageResult:
    """`v3_models`/`v3_max_records` default to the full check shape (both
    model families, every flagged record plus a per-contract stratified
    sample) -- both exist as overrides for a budget-capped run
    (e.g. one cheap model, a hard cap on how many records get a V3 call),
    not as a permanent change to the default shape.
    """
    settings = get_settings()
    settings.ensure_dirs()
    engine = get_engine()
    models = v3_models if v3_models is not None else (settings.anthropic_spec, settings.openai_spec)

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        already = has_checks_for_run(conn, run_id)
        if not force and already:
            return StageResult(stage=STAGE, status="skipped", counts={})
        if force:
            # Gating this on `already` (rather than `force` alone) looks
            # equivalent but isn't: S6 --force cascade-deletes check_results
            # as a side effect of re-inserting association_records (see
            # store/records.py's delete_records_for_run), which can leave
            # `already` False here even though this run's OWN prior
            # llm_calls are still sitting in the table, orphaned -- found by
            # a real re-run producing exactly double the expected cost
            # (400 llm_calls instead of 200) after an upstream S6 fix.
            delete_checks_for_run(conn, run_id)
            delete_llm_calls_for_run(conn, run_id, stage=STAGE)

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S8 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)

        records = list_records_for_run(conn, run_id)
        bundle = get_methods_bundle_for_run(conn, run_id)
        methods_excerpt = str(bundle["content"]) if bundle else "(no methods text available)"

        by_contract: dict[str, list[dict[str, Any]]] = {}
        for r in records:
            by_contract.setdefault(r["schema_contract_id"], []).append(r)

        counts: dict[str, int] = {
            "records_checked": len(records),
            "v1_fail": 0,
            "v1_pass": 0,
            "v1_skip": 0,
            "v2_contract_failed": 0,
            "v2_mismatch": 0,
            "v2_pass": 0,
            "v2_skip": 0,
            "v3_records_evaluated": 0,
            "v3_llm_failures": 0,
        }

        check_rows: list[dict[str, Any]] = []
        v3_candidates: set[str] = set()  # record_ids that must get V3
        v3_stratified: list[dict[str, Any]] = []

        # --- V1: every record, pure code ---
        for r in records:
            result = _v1_arithmetic(r)
            check_rows.append(
                {
                    "record_id": r["record_id"],
                    "check_name": "v1_arithmetic",
                    "status": result["status"],
                    "detail": result["detail"],
                    "checked_by": "code",
                }
            )
            counts[f"v1_{result['status']}"] += 1
            if result["status"] == "fail":
                v3_candidates.add(r["record_id"])

        # --- V2: per contract, sampled ---
        for contract_id, contract_records in by_contract.items():
            mappings = list_column_mappings(conn, contract_id)
            mappings_by_field: dict[str, dict[str, Any]] = {}
            for m in mappings:
                if m["target_field"] in IDENTITY_TARGET_FIELDS:
                    mappings_by_field[m["target_field"]] = m

            sample = (
                contract_records
                if len(contract_records) <= V2_SAMPLE_SIZE
                else random.sample(contract_records, V2_SAMPLE_SIZE)
            )
            sampled_ids = {r["record_id"] for r in sample}
            checked = 0
            mismatched = 0
            per_record_mismatch: dict[str, bool] = {}
            for r in sample:
                mismatch = _identity_field_mismatch(r, mappings_by_field, conn)
                if mismatch is None:
                    continue
                checked += 1
                per_record_mismatch[r["record_id"]] = mismatch
                if mismatch:
                    mismatched += 1

            contract_failed = checked > 0 and (mismatched / checked) > V2_MISMATCH_THRESHOLD
            if contract_failed:
                counts["v2_contract_failed"] += 1
                detail = (
                    f"contract-level grounding mismatch rate {mismatched}/{checked} "
                    f"({mismatched / checked:.1%}) exceeds {V2_MISMATCH_THRESHOLD:.0%} "
                    "threshold -- likely a row-shift from a mis-detected header"
                )
                for r in contract_records:
                    check_rows.append(
                        {
                            "record_id": r["record_id"],
                            "check_name": "v2_grounding",
                            "status": "fail",
                            "detail": detail,
                            "checked_by": "code",
                        }
                    )
                    counts["v2_mismatch"] += 1
                    v3_candidates.add(r["record_id"])
            else:
                for r in contract_records:
                    rid = r["record_id"]
                    if rid not in sampled_ids or rid not in per_record_mismatch:
                        check_rows.append(
                            {
                                "record_id": r["record_id"],
                                "check_name": "v2_grounding",
                                "status": "skip",
                                "detail": "not sampled, or no identity field backed by an "
                                "identity-transform column mapping",
                                "checked_by": "code",
                            }
                        )
                        counts["v2_skip"] += 1
                    elif per_record_mismatch[r["record_id"]]:
                        # Below the contract-level threshold -- a single
                        # sampled row disagreeing is worth a note, not a
                        # verdict that the whole contract is misaligned.
                        check_rows.append(
                            {
                                "record_id": r["record_id"],
                                "check_name": "v2_grounding",
                                "status": "warn",
                                "detail": "stored value doesn't match the source cell, but the "
                                "contract's overall mismatch rate is below threshold",
                                "checked_by": "code",
                            }
                        )
                        counts["v2_pass"] += 1  # counted as pass at the contract level
                        v3_candidates.add(r["record_id"])
                    else:
                        check_rows.append(
                            {
                                "record_id": r["record_id"],
                                "check_name": "v2_grounding",
                                "status": "pass",
                                "detail": "stored value matches the source cell",
                                "checked_by": "code",
                            }
                        )
                        counts["v2_pass"] += 1

            # Stratified V3 sample for this contract, on top of anything V1/V2 already flagged.
            pool = [r for r in contract_records if r["record_id"] not in v3_candidates]
            v3_stratified.extend(
                random.sample(pool, min(V3_SAMPLE_PER_CONTRACT, len(pool))) if pool else []
            )

        records_by_id = {r["record_id"]: r for r in records}
        flagged = [records_by_id[rid] for rid in v3_candidates]
        v3_targets = flagged + v3_stratified
        counts["v3_targets_uncapped"] = len(v3_targets)

        if v3_max_records is not None and len(v3_targets) > v3_max_records:
            if len(flagged) >= v3_max_records:
                v3_targets = random.sample(flagged, v3_max_records)
            else:
                remaining = v3_max_records - len(flagged)
                v3_targets = flagged + random.sample(
                    v3_stratified, min(remaining, len(v3_stratified))
                )
            counts["v3_targets_dropped_by_cap"] = counts["v3_targets_uncapped"] - len(v3_targets)

    # --- V3: bounded concurrency, one call per record per model in `models` ---
    if v3_targets:
        interpretation_by_contract: dict[str, str] = {}
        with engine.begin() as conn:
            for contract_id in {r["schema_contract_id"] for r in v3_targets}:
                contract = get_schema_contract(conn, contract_id)
                interpretation_by_contract[contract_id] = (
                    str(contract["interpretation_notes"]) if contract else ""
                )

        sem = asyncio.Semaphore(V3_CONCURRENCY)

        async def evaluate(record: dict[str, Any]) -> None:
            async with sem:
                system, user = render_prompt(
                    "s8_v3_semantic",
                    row_block=_render_row_block(record),
                    interpretation_notes=interpretation_by_contract.get(
                        record["schema_contract_id"], ""
                    ),
                    methods_excerpt=methods_excerpt,
                )
                for model in models:
                    verdict, llm_record = await _run_v3(
                        settings=settings,
                        model=model,
                        system=system,
                        user=user,
                        record_id=record["record_id"],
                    )
                    with engine.begin() as conn:
                        insert_llm_call(
                            conn,
                            run_id=run_id,
                            stage=STAGE,
                            entity_id=llm_record.entity_id,
                            provider=llm_record.model_spec.provider,
                            model=llm_record.model_spec.model,
                            prompt_hash=llm_record.prompt_hash,
                            prompt=llm_record.prompt,
                            response=llm_record.response,
                            input_tokens=llm_record.input_tokens,
                            output_tokens=llm_record.output_tokens,
                            cost_usd=llm_record.cost_usd,
                            latency_ms=llm_record.latency_ms,
                            ok=llm_record.ok,
                        )
                        if verdict is not None:
                            insert_check_results(
                                conn, _v3_check_results(record["record_id"], model, verdict)
                            )
                        else:
                            counts["v3_llm_failures"] += 1
                counts["v3_records_evaluated"] += 1

        await asyncio.gather(*(evaluate(r) for r in v3_targets))

    status: StageStatus = "done"
    with engine.begin() as conn:
        insert_check_results(conn, check_rows)
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S8 validation finished: {counts['records_checked']} records -- "
            f"V1 {counts['v1_fail']} fail/{counts['v1_pass']} pass, "
            f"V2 {counts['v2_contract_failed']} contracts failed, "
            f"V3 {counts['v3_records_evaluated']} records evaluated",
            payload=counts,
        )

    return StageResult(stage=STAGE, status=status, counts=counts)
