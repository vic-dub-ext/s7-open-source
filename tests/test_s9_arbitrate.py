import pytest

from s7.stages import s9_arbitrate
from s7.stages.s9_arbitrate import _arbitrate, _v3_verdict
from s7.store.artifacts import insert_artifact
from s7.store.checks import insert_check_results, list_check_results_for_record
from s7.store.classifications import insert_classification
from s7.store.contracts import insert_column_mappings, insert_schema_contract
from s7.store.db import get_engine, new_id, now_iso
from s7.store.parsed import insert_parsed_table
from s7.store.records import insert_association_records, list_records_for_run
from s7.store.runs import create_run

BOTH_PASS = [
    {"check_name": "v3_semantic_effect_allele", "status": "pass", "checked_by": "llm:a"},
    {"check_name": "v3_semantic_effect_allele", "status": "pass", "checked_by": "llm:b"},
]
BOTH_FAIL = [
    {"check_name": "v3_semantic_effect_allele", "status": "fail", "checked_by": "llm:a"},
    {"check_name": "v3_semantic_effect_allele", "status": "fail", "checked_by": "llm:b"},
]
DISAGREE = [
    {"check_name": "v3_semantic_effect_allele", "status": "pass", "checked_by": "llm:a"},
    {"check_name": "v3_semantic_effect_allele", "status": "fail", "checked_by": "llm:b"},
]
SINGLE_MODEL_FAIL = [
    {"check_name": "v3_semantic_effect_allele", "status": "fail", "checked_by": "llm:a"},
]


def test_v3_verdict_both_pass_is_neither_fail_nor_disagree() -> None:
    both_fail, disagree = _v3_verdict(BOTH_PASS)
    assert both_fail is False
    assert disagree is False


def test_v3_verdict_both_fail() -> None:
    both_fail, disagree = _v3_verdict(BOTH_FAIL)
    assert both_fail is True
    assert disagree is False


def test_v3_verdict_disagreement() -> None:
    both_fail, disagree = _v3_verdict(DISAGREE)
    assert both_fail is False
    assert disagree is True


def test_v3_verdict_single_model_call_is_neither() -> None:
    # A capped/single-model V3 run has only one verdict per question -- it
    # can't disagree with or duplicate-fail against a verdict that doesn't exist.
    both_fail, disagree = _v3_verdict(SINGLE_MODEL_FAIL)
    assert both_fail is False
    assert disagree is False


def test_arbitrate_clean_record_is_auto_pass() -> None:
    confidence, status = _arbitrate(
        checks=[{"check_name": "v1_arithmetic", "status": "pass", "checked_by": "code"}],
        contract_confidence=0.9,
        classification_confidence=0.95,
    )
    assert status == "auto_pass"
    assert confidence == pytest.approx(0.9 * 0.95)


def test_arbitrate_auto_pass_confidence_is_floored_at_0_6() -> None:
    confidence, status = _arbitrate(
        checks=[], contract_confidence=0.65, classification_confidence=0.76
    )
    assert status == "auto_pass"
    assert confidence == 0.6  # 0.65 * 0.76 = 0.494, floored up to 0.6


def test_arbitrate_v1_fail_caps_confidence_and_needs_review() -> None:
    confidence, status = _arbitrate(
        checks=[{"check_name": "v1_arithmetic", "status": "fail", "checked_by": "code"}],
        contract_confidence=0.9,
        classification_confidence=0.9,
    )
    assert status == "needs_review"
    assert confidence <= s9_arbitrate.V1_FAIL_CEILING


def test_arbitrate_v2_contract_fail_caps_tighter_than_v1() -> None:
    confidence, status = _arbitrate(
        checks=[{"check_name": "v2_grounding", "status": "fail", "checked_by": "code"}],
        contract_confidence=0.9,
        classification_confidence=0.9,
    )
    assert status == "needs_review"
    assert confidence <= s9_arbitrate.V2_CONTRACT_FAIL_CEILING


def test_arbitrate_v3_disagreement_needs_review() -> None:
    confidence, status = _arbitrate(
        checks=DISAGREE, contract_confidence=0.9, classification_confidence=0.9
    )
    assert status == "needs_review"
    assert confidence <= s9_arbitrate.V3_DISAGREEMENT_CEILING


def test_arbitrate_v3_both_fail_is_rejected_not_needs_review() -> None:
    confidence, status = _arbitrate(
        checks=BOTH_FAIL, contract_confidence=0.9, classification_confidence=0.9
    )
    assert status == "rejected"
    assert confidence <= s9_arbitrate.V3_BOTH_FAIL_CEILING


def test_arbitrate_low_contract_confidence_needs_review() -> None:
    confidence, status = _arbitrate(
        checks=[], contract_confidence=0.4, classification_confidence=0.9
    )
    assert status == "needs_review"


def test_arbitrate_low_classification_confidence_needs_review() -> None:
    confidence, status = _arbitrate(
        checks=[], contract_confidence=0.9, classification_confidence=0.5
    )
    assert status == "needs_review"


def test_arbitrate_missing_classification_confidence_is_not_penalized() -> None:
    # No matching sheet_classification -- treated as "no signal," not an
    # automatic needs_review (see s9_arbitrate.py's docstring).
    confidence, status = _arbitrate(
        checks=[], contract_confidence=0.9, classification_confidence=None
    )
    assert status == "auto_pass"
    assert confidence == pytest.approx(max(0.9, 0.6))


def test_arbitrate_worst_signal_wins_over_best() -> None:
    # V1 fail (ceiling 0.3) and V2 contract fail (ceiling 0.2) together ->
    # the stricter ceiling applies, not the first one evaluated.
    confidence, status = _arbitrate(
        checks=[
            {"check_name": "v1_arithmetic", "status": "fail", "checked_by": "code"},
            {"check_name": "v2_grounding", "status": "fail", "checked_by": "code"},
        ],
        contract_confidence=0.9,
        classification_confidence=0.9,
    )
    assert status == "needs_review"
    assert confidence <= s9_arbitrate.V2_CONTRACT_FAIL_CEILING


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


def _seed_run_with_one_record(
    conn, *, v1_status: str, classification_confidence: float
) -> tuple[str, str]:
    run_id = create_run(conn, paper_key="test", doi="10.1/test")
    artifact_id = insert_artifact(
        conn,
        run_id=run_id,
        kind="sheet",
        file_name="t.xlsx",
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        byte_size=10,
        sha256="abc",
        storage_path="",
        retrieved_at=now_iso(),
        sheet_name="ST1",
    )
    insert_classification(
        conn,
        run_id=run_id,
        artifact_id=artifact_id,
        classification_id="genetic_associations",
        confidence=classification_confidence,
        insights="",
        processor_id="p1",
        processor_version="1.0",
        retried=False,
        needs_review=False,
    )
    table_id = insert_parsed_table(
        conn,
        run_id=run_id,
        artifact_id=artifact_id,
        extend_parse_run_id="pr_1",
        target="markdown",
        content=None,
        header_rows=[["Gene", "Trait"]],
        row_count=1,
        col_count=2,
        raw_response_path="",
        created_at=now_iso(),
    )
    contract_id = insert_schema_contract(
        conn,
        parsed_table_id=table_id,
        model_spec="anthropic:claude-sonnet-5",
        row_entity="gene",
        constant_fields={},
        effect_allele_source="unresolvable",
        effect_allele_column=None,
        unmapped_columns=[],
        interpretation_notes="test",
        overall_confidence=0.9,
        needs_review=False,
        agreement_group_id=new_id(),
    )
    insert_column_mappings(
        conn,
        contract_id,
        [
            {
                "source_column": "Gene",
                "source_column_index": 0,
                "target_field": "gene_symbol_raw",
                "evidence": "e",
                "confidence": 0.9,
            }
        ],
    )
    record_id = "rec-1"
    insert_association_records(
        conn,
        [
            {
                "record_id": record_id,
                "run_id": run_id,
                "pipeline_version": "0.1.0",
                "extracted_at": now_iso(),
                "source_doi": "10.1/test",
                "source_file_name": "t.xlsx",
                "source_file_sha256": "abc",
                "source_row_index": 0,
                "source_parsed_table_id": table_id,
                "extend_parse_run_id": "pr_1",
                "schema_contract_id": contract_id,
                "entity_type": "gene",
                "gene_symbol_raw": "BRCA1",
                "trait_raw": "Breast cancer",
                # S6's provisional placeholder, per s6_project.py.
                "confidence": 0.9,
                "review_status": "needs_review",
            }
        ],
    )
    insert_check_results(
        conn,
        [
            {
                "record_id": record_id,
                "check_name": "v1_arithmetic",
                "status": v1_status,
                "detail": "test",
                "checked_by": "code",
            }
        ],
    )
    return run_id, record_id


async def test_run_writes_auto_pass_for_clean_record(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id, record_id = _seed_run_with_one_record(
            conn, v1_status="pass", classification_confidence=0.95
        )

    result = await s9_arbitrate.run(run_id)
    assert result.status == "done"
    assert result.counts["auto_pass"] == 1

    with engine.begin() as conn:
        records = list_records_for_run(conn, run_id)
    assert records[0]["review_status"] == "auto_pass"
    assert records[0]["confidence"] == pytest.approx(max(0.9 * 0.95, 0.6))


async def test_run_writes_needs_review_for_v1_failure(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id, record_id = _seed_run_with_one_record(
            conn, v1_status="fail", classification_confidence=0.95
        )

    await s9_arbitrate.run(run_id)

    with engine.begin() as conn:
        records = list_records_for_run(conn, run_id)
    assert records[0]["review_status"] == "needs_review"
    assert records[0]["confidence"] <= s9_arbitrate.V1_FAIL_CEILING


async def test_run_replaces_s6s_provisional_placeholder(db) -> None:
    # S6 stamps every record review_status="needs_review" as a placeholder
    # (see s6_project.py) -- confirm S9 overwrites it even for a clean record
    # that ends up auto_pass, not just records that stay needs_review.
    engine = db
    with engine.begin() as conn:
        run_id, record_id = _seed_run_with_one_record(
            conn, v1_status="pass", classification_confidence=0.95
        )

    await s9_arbitrate.run(run_id)

    with engine.begin() as conn:
        records = list_records_for_run(conn, run_id)
    assert records[0]["review_status"] != "needs_review"


async def test_run_skips_without_force_once_finished(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id, _ = _seed_run_with_one_record(
            conn, v1_status="pass", classification_confidence=0.95
        )

    await s9_arbitrate.run(run_id)
    result = await s9_arbitrate.run(run_id)
    assert result.status == "skipped"


async def test_run_is_idempotent_on_force(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id, record_id = _seed_run_with_one_record(
            conn, v1_status="pass", classification_confidence=0.95
        )

    await s9_arbitrate.run(run_id)
    result = await s9_arbitrate.run(run_id, force=True)
    assert result.status == "done"
    assert result.counts["records_arbitrated"] == 1

    with engine.begin() as conn:
        checks = list_check_results_for_record(conn, record_id)
    # S9 never touches check_results -- only association_records.
    assert len(checks) == 1
