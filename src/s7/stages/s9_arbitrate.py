"""S9 - Arbitration and routing. Combines S8's check_results with S5's
contract confidence and S3's classification confidence into one
`confidence` float and one `review_status` per record, replacing S6's
provisional placeholder (review_status="needs_review", confidence=contract
confidence -- see s6_project.py). The exact formula lives in one place, in
code, with a comment explaining each term, rather than being scattered:
all of it is in `_arbitrate` below.

Like S7, this stage only ever UPDATEs association_records rows S6 already
inserted -- it never inserts new ones -- so idempotency is checked via
events.has_stage_finished rather than row existence (see runs.py's
stage_counts for the matching UI-side gate).
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any

from s7.models.record import ReviewStatus
from s7.models.stage import StageResult, StageStatus
from s7.store import events
from s7.store.checks import list_check_results_for_run
from s7.store.classifications import list_classifications_for_run
from s7.store.contracts import list_contracts_for_run
from s7.store.db import get_engine
from s7.store.parsed import list_parsed_tables_for_run
from s7.store.records import list_records_for_run, update_arbitration
from s7.store.runs import get_run, update_run_status

STAGE = "s9_arbitrate"

CLASSIFICATION_CONFIDENCE_THRESHOLD = 0.75
CONTRACT_CONFIDENCE_THRESHOLD = 0.6
AUTO_PASS_FLOOR = 0.6
V1_FAIL_CEILING = 0.3
V2_CONTRACT_FAIL_CEILING = 0.2
V3_DISAGREEMENT_CEILING = 0.5
# "V3 both fail -> rejected" has no inherent ceiling, unlike the rules above
# -- this one is chosen so a rejected record's confidence still sorts below
# every needs_review ceiling above, keeping the
# UI's low-to-high ordering meaningful even though rejected records are
# excluded from S10's published dataset regardless of the exact number.
V3_BOTH_FAIL_CEILING = 0.1

V3_QUESTIONS = (
    "v3_semantic_effect_allele",
    "v3_semantic_effect_direction",
    "v3_semantic_trait_type",
    "v3_semantic_analysis_role",
)


def _v3_verdict(checks: list[dict[str, Any]]) -> tuple[bool, bool]:
    """(any_both_models_fail, any_models_disagree) across the four V3
    questions for one record's checks. Grouped by (question, checked_by) so
    a capped or single-model V3 run -- fewer than two verdicts for a
    question -- can neither "disagree" nor "both fail" with itself.
    """
    by_question: dict[str, dict[str, str]] = defaultdict(dict)
    for c in checks:
        if c["check_name"] in V3_QUESTIONS:
            by_question[c["check_name"]][c["checked_by"]] = c["status"]

    both_fail = False
    disagree = False
    for statuses in by_question.values():
        if len(statuses) < 2:
            continue
        distinct = set(statuses.values())
        if distinct == {"fail"}:
            both_fail = True
        elif len(distinct) > 1:
            disagree = True
    return both_fail, disagree


def _arbitrate(
    *,
    checks: list[dict[str, Any]],
    contract_confidence: float,
    classification_confidence: float | None,
) -> tuple[float, ReviewStatus]:
    """The one place the routing formula lives. Each rule below is a
    ceiling on the final confidence, not an assignment -- a record can trip
    several at once (e.g. a V1 failure in a low-confidence contract), and
    the final confidence is the *strictest* applicable ceiling, so the worst
    signal always wins rather than the last one evaluated.

    `classification_confidence` is None when a record's source table has no
    matching sheet_classification (shouldn't happen once S6 always stamps
    source_parsed_table_id, but the join is defensive) -- treated as
    "no signal" rather than an automatic needs_review, since penalizing for
    data we don't have would conflate "missing" with "bad."
    """
    v1_failed = any(c["check_name"] == "v1_arithmetic" and c["status"] == "fail" for c in checks)
    v2_contract_failed = any(
        c["check_name"] == "v2_grounding" and c["status"] == "fail" for c in checks
    )
    v3_both_fail, v3_disagree = _v3_verdict(checks)

    base = contract_confidence * (
        classification_confidence if classification_confidence is not None else 1.0
    )

    ceilings = [1.0]  # neutral upper bound if nothing below applies
    if v1_failed:
        ceilings.append(V1_FAIL_CEILING)
    if v2_contract_failed:
        ceilings.append(V2_CONTRACT_FAIL_CEILING)
    if v3_disagree:
        ceilings.append(V3_DISAGREEMENT_CEILING)
    if v3_both_fail:
        ceilings.append(V3_BOTH_FAIL_CEILING)

    if v3_both_fail:
        # A flat "no" from every model that looked at it is stronger than
        # mere uncertainty -- reject outright rather than route for review.
        return min(base, *ceilings), "rejected"

    needs_review = (
        v1_failed
        or v2_contract_failed
        or v3_disagree
        or contract_confidence < CONTRACT_CONFIDENCE_THRESHOLD
        or (
            classification_confidence is not None
            and classification_confidence < CLASSIFICATION_CONFIDENCE_THRESHOLD
        )
    )
    if needs_review:
        return min(base, *ceilings), "needs_review"

    return max(base, AUTO_PASS_FLOOR), "auto_pass"


async def run(run_id: str, *, force: bool = False) -> StageResult:
    engine = get_engine()

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        already = events.has_stage_finished(conn, run_id, STAGE)
        if not force and already:
            return StageResult(stage=STAGE, status="skipped", counts={})

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S9 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)

        records = list_records_for_run(conn, run_id)
        checks = list_check_results_for_run(conn, run_id)
        contracts = list_contracts_for_run(conn, run_id)
        classifications = list_classifications_for_run(conn, run_id)
        parsed_tables = list_parsed_tables_for_run(conn, run_id)

        contract_confidence_by_id = {
            c["id"]: float(c["overall_confidence"]) for c in contracts
        }
        # Effective classification confidence per artifact -- a human
        # override changes the class label, not the model's own confidence
        # in that call, so the classification-confidence threshold reads the
        # raw model confidence regardless of override.
        classification_confidence_by_artifact = {
            c["artifact_id"]: float(c["confidence"]) for c in classifications
        }
        artifact_id_by_parsed_table = {
            pt["id"]: pt["artifact_id"] for pt in parsed_tables
        }

        checks_by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in checks:
            checks_by_record[c["record_id"]].append(c)

        counts: dict[str, int] = {
            "records_arbitrated": 0,
            "auto_pass": 0,
            "needs_review": 0,
            "rejected": 0,
        }
        updates: list[dict[str, Any]] = []
        for r in records:
            contract_confidence = contract_confidence_by_id.get(r["schema_contract_id"], 0.0)
            artifact_id = artifact_id_by_parsed_table.get(r["source_parsed_table_id"])
            classification_confidence = (
                classification_confidence_by_artifact.get(artifact_id)
                if artifact_id is not None
                else None
            )
            confidence, review_status = _arbitrate(
                checks=checks_by_record.get(r["record_id"], []),
                contract_confidence=contract_confidence,
                classification_confidence=classification_confidence,
            )
            updates.append(
                {
                    "record_id": r["record_id"],
                    "confidence": confidence,
                    "review_status": review_status,
                }
            )
            counts["records_arbitrated"] += 1
            counts[review_status] += 1

        update_arbitration(conn, updates)

        status: StageStatus = "done"
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S9 arbitration finished: {counts['records_arbitrated']} records -- "
            f"{counts['auto_pass']} auto_pass, {counts['needs_review']} needs_review, "
            f"{counts['rejected']} rejected",
            payload=counts,
        )

    return StageResult(stage=STAGE, status=status, counts=counts)
