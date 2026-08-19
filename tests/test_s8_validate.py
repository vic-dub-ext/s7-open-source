import pytest

from s7.config import ModelSpec
from s7.models.validation import V3SemanticVerdict
from s7.stages import s8_validate
from s7.stages.s8_validate import (
    _identity_field_mismatch,
    _render_row_block,
    _v1_arithmetic,
    _v3_check_results,
)
from s7.store.artifacts import insert_artifact
from s7.store.contracts import insert_column_mappings, insert_schema_contract
from s7.store.db import get_engine, new_id, now_iso
from s7.store.parsed import insert_parsed_cells, insert_parsed_table
from s7.store.records import insert_association_records
from s7.store.runs import create_run


def test_v1_arithmetic_passes_when_p_matches_effect_over_se() -> None:
    # z = 4/2 = 2 -> two-sided p ~= 0.0455
    result = _v1_arithmetic({"effect_value": 4.0, "standard_error": 2.0, "p_value": 0.0455})
    assert result["status"] == "pass"


def test_v1_arithmetic_fails_when_p_wildly_inconsistent() -> None:
    result = _v1_arithmetic({"effect_value": 4.0, "standard_error": 2.0, "p_value": 0.9})
    assert result["status"] == "fail"
    assert "recomputed" in result["detail"]


def test_v1_arithmetic_fails_on_out_of_range_p_value() -> None:
    result = _v1_arithmetic({"p_value": 1.5})
    assert result["status"] == "fail"
    assert "p_value" in result["detail"]


def test_v1_arithmetic_fails_on_non_positive_se() -> None:
    result = _v1_arithmetic({"standard_error": -1.0})
    assert result["status"] == "fail"


def test_v1_arithmetic_fails_on_non_positive_odds_ratio() -> None:
    result = _v1_arithmetic({"effect_type": "odds_ratio", "effect_value": -0.5})
    assert result["status"] == "fail"


def test_v1_arithmetic_fails_when_cases_plus_controls_exceed_total() -> None:
    result = _v1_arithmetic({"n_total": 100, "n_cases": 60, "n_controls": 60})
    assert result["status"] == "fail"


def test_v1_arithmetic_fails_when_carriers_exceed_total() -> None:
    result = _v1_arithmetic({"n_total": 100, "n_carriers": 150})
    assert result["status"] == "fail"


def test_v1_arithmetic_fails_on_maf_out_of_range() -> None:
    result = _v1_arithmetic({"maf_threshold": 1.5})
    assert result["status"] == "fail"


def test_v1_arithmetic_fails_when_effect_outside_ci() -> None:
    result = _v1_arithmetic({"effect_value": 5.0, "ci_lower": 1.0, "ci_upper": 2.0})
    assert result["status"] == "fail"
    assert "outside reported CI" in result["detail"]


def test_v1_arithmetic_passes_when_effect_within_ci() -> None:
    result = _v1_arithmetic({"effect_value": 1.5, "ci_lower": 1.0, "ci_upper": 2.0})
    assert result["status"] == "pass"


def test_v1_arithmetic_skips_when_nothing_applicable() -> None:
    result = _v1_arithmetic({"gene_symbol_raw": "BRCA1"})
    assert result["status"] == "skip"


def test_render_row_block_excludes_none_fields() -> None:
    block = _render_row_block({"gene_symbol_raw": "BRCA1", "trait_raw": None, "p_value": 0.01})
    assert "gene_symbol_raw: BRCA1" in block
    assert "trait_raw" not in block
    assert "p_value: 0.01" in block


def test_v3_check_results_maps_verdict_to_four_rows() -> None:
    verdict = V3SemanticVerdict(
        effect_allele_correct=True,
        effect_allele_reasoning="looks right",
        effect_direction_consistent=False,
        effect_direction_reasoning="sign mismatch",
        trait_type_and_effect_type_appropriate=True,
        trait_type_reasoning="fine",
        analysis_role_correct=True,
        analysis_role_reasoning="fine",
    )
    model = ModelSpec(provider="anthropic", model="claude-sonnet-5")
    rows = _v3_check_results("rec-1", model, verdict)
    assert len(rows) == 4
    by_name = {r["check_name"]: r for r in rows}
    assert by_name["v3_semantic_effect_allele"]["status"] == "pass"
    assert by_name["v3_semantic_effect_direction"]["status"] == "fail"
    assert by_name["v3_semantic_effect_direction"]["detail"] == "sign mismatch"
    assert all(r["checked_by"] == "llm:anthropic:claude-sonnet-5" for r in rows)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


def test_identity_field_mismatch_detects_match(db) -> None:
    engine = db
    with engine.begin() as conn:
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
        insert_parsed_cells(
            conn,
            table_id,
            [
                {"row_index": 0, "col_index": 0, "value": "BRCA1"},
                {"row_index": 0, "col_index": 1, "value": "Breast cancer"},
            ],
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
            overall_confidence=0.8,
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
                },
                {
                    "source_column": "Trait",
                    "source_column_index": 1,
                    "target_field": "trait_raw",
                    "evidence": "e",
                    "confidence": 0.9,
                },
            ],
        )
        mappings = {
            m["target_field"]: m
            for m in [
                {"target_field": "gene_symbol_raw", "source_column_index": 0, "transform": None},
                {"target_field": "trait_raw", "source_column_index": 1, "transform": None},
            ]
        }
        matching_record = {
            "source_parsed_table_id": table_id,
            "source_row_index": 0,
            "gene_symbol_raw": "BRCA1",
            "trait_raw": "Breast cancer",
        }
        assert _identity_field_mismatch(matching_record, mappings, conn) is False

        mismatching_record = {
            "source_parsed_table_id": table_id,
            "source_row_index": 0,
            "gene_symbol_raw": "WRONG_GENE",
            "trait_raw": "Breast cancer",
        }
        assert _identity_field_mismatch(mismatching_record, mappings, conn) is True

        unchecked_record = {"source_parsed_table_id": None, "source_row_index": 0}
        assert _identity_field_mismatch(unchecked_record, {}, conn) is None


async def _fake_run_v3(*, settings, model, system, user, record_id):
    from datetime import UTC, datetime

    from s7.providers.llm import LLMCallRecord

    verdict = V3SemanticVerdict(
        effect_allele_correct=True,
        effect_allele_reasoning="ok",
        effect_direction_consistent=True,
        effect_direction_reasoning="ok",
        trait_type_and_effect_type_appropriate=True,
        trait_type_reasoning="ok",
        analysis_role_correct=True,
        analysis_role_reasoning="ok",
    )
    record = LLMCallRecord(
        model_spec=model,
        prompt_hash="h",
        prompt=user,
        response="{}",
        input_tokens=1,
        output_tokens=1,
        cost_usd=0.0,
        latency_ms=1,
        stage=s8_validate.STAGE,
        entity_id=record_id,
        ok=True,
        created_at=datetime.now(UTC),
    )
    return verdict, record


def _seed_run_with_one_record(conn, *, needs_v3: bool) -> tuple[str, str]:
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
    table_id = insert_parsed_table(
        conn,
        run_id=run_id,
        artifact_id=artifact_id,
        extend_parse_run_id="pr_1",
        target="markdown",
        content=None,
        header_rows=[["Gene", "Trait", "P"]],
        row_count=1,
        col_count=3,
        raw_response_path="",
        created_at=now_iso(),
    )
    insert_parsed_cells(
        conn,
        table_id,
        [
            {"row_index": 0, "col_index": 0, "value": "BRCA1"},
            {"row_index": 0, "col_index": 1, "value": "Breast cancer"},
            {"row_index": 0, "col_index": 2, "value": "0.05"},
        ],
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
        overall_confidence=0.8,
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
    # A p_value wildly inconsistent with 0 effect/SE triggers V1 fail -> V3 candidate,
    # when needs_v3 is True; otherwise a clean record with nothing to flag.
    p_value = 0.9 if needs_v3 else None
    insert_association_records(
        conn,
        [
            {
                "record_id": "rec-1",
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
                "effect_value": 4.0 if needs_v3 else None,
                "standard_error": 2.0 if needs_v3 else None,
                "p_value": p_value,
                "confidence": 0.8,
                "review_status": "needs_review",
            }
        ],
    )
    return run_id, "rec-1"


async def test_run_writes_v1_and_v2_checks_for_every_record(db, monkeypatch) -> None:
    monkeypatch.setattr(s8_validate, "_run_v3", _fake_run_v3)
    engine = db
    with engine.begin() as conn:
        run_id, record_id = _seed_run_with_one_record(conn, needs_v3=False)

    result = await s8_validate.run(run_id)
    assert result.status == "done"
    assert result.counts["records_checked"] == 1

    from s7.store.checks import list_check_results_for_record

    with engine.begin() as conn:
        checks = list_check_results_for_record(conn, record_id)
    names = {c["check_name"] for c in checks}
    assert "v1_arithmetic" in names
    assert "v2_grounding" in names


async def test_run_evaluates_v3_for_v1_flagged_records(db, monkeypatch) -> None:
    monkeypatch.setattr(s8_validate, "_run_v3", _fake_run_v3)
    engine = db
    with engine.begin() as conn:
        run_id, record_id = _seed_run_with_one_record(conn, needs_v3=True)

    result = await s8_validate.run(run_id)
    assert result.counts["v3_records_evaluated"] == 1

    from s7.store.checks import list_check_results_for_record

    with engine.begin() as conn:
        checks = list_check_results_for_record(conn, record_id)
    v3_names = {c["check_name"] for c in checks if c["check_name"].startswith("v3_")}
    assert v3_names == {
        "v3_semantic_effect_allele",
        "v3_semantic_effect_direction",
        "v3_semantic_trait_type",
        "v3_semantic_analysis_role",
    }
    # Two model families -> two rows per question.
    assert len([c for c in checks if c["check_name"] == "v3_semantic_effect_allele"]) == 2


async def test_run_skips_without_force_once_checks_exist(db, monkeypatch) -> None:
    monkeypatch.setattr(s8_validate, "_run_v3", _fake_run_v3)
    engine = db
    with engine.begin() as conn:
        run_id, _ = _seed_run_with_one_record(conn, needs_v3=False)

    await s8_validate.run(run_id)
    result = await s8_validate.run(run_id)
    assert result.status == "skipped"


async def test_run_is_idempotent_on_force(db, monkeypatch) -> None:
    # Note: with only one record in the whole run, the per-contract
    # stratified V3 sample (independent of V1/V2 flags) always includes it,
    # so this also exercises the --force path clearing prior V3 check rows.
    monkeypatch.setattr(s8_validate, "_run_v3", _fake_run_v3)
    engine = db
    with engine.begin() as conn:
        run_id, record_id = _seed_run_with_one_record(conn, needs_v3=False)

    from s7.store.checks import list_check_results_for_record

    await s8_validate.run(run_id)
    with engine.begin() as conn:
        checks_after_first = list_check_results_for_record(conn, record_id)

    await s8_validate.run(run_id, force=True)
    with engine.begin() as conn:
        checks_after_second = list_check_results_for_record(conn, record_id)

    assert len(checks_after_second) == len(checks_after_first)
