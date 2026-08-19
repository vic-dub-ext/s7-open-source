
import pytest

from s7.stages import s6_project
from s7.stages.s6_project import (
    _apply_transform,
    _build_record_fields,
    _coerce_field_value,
    _coerce_numeric,
    _is_complete,
    _parse_ci_string,
    _resolve_column_index,
    _select_primary_contract,
)
from s7.store.artifacts import insert_artifact
from s7.store.classifications import insert_classification
from s7.store.contracts import (
    insert_column_mappings,
    insert_contract_table_members,
    insert_schema_contract,
)
from s7.store.db import get_engine, new_id, now_iso
from s7.store.parsed import insert_parsed_cells, insert_parsed_table
from s7.store.records import count_records_for_run
from s7.store.runs import create_run


def test_coerce_numeric_parses_clean_float() -> None:
    assert _coerce_numeric("1.24000E-24") == pytest.approx(1.24e-24)


def test_coerce_numeric_extracts_leading_number_from_composite_string() -> None:
    # Real backman2021 data: "Effect (95% CI)" mapped with transform=identity
    # produces exactly this shape.
    assert _coerce_numeric("0.116[0.099,0.132]") == pytest.approx(0.116)


def test_coerce_numeric_returns_none_for_unparseable() -> None:
    assert _coerce_numeric("not a number") is None
    assert _coerce_numeric("") is None


def test_parse_ci_string_extracts_last_two_numbers() -> None:
    assert _parse_ci_string("0.116[0.099,0.132]") == (0.099, 0.132)


def test_parse_ci_string_handles_parenthetical_form() -> None:
    assert _parse_ci_string("(1.2, 3.4)") == (1.2, 3.4)


def test_parse_ci_string_orders_bounds_ascending_even_if_swapped() -> None:
    assert _parse_ci_string("[3.4, 1.2]") == (1.2, 3.4)


def test_parse_ci_string_returns_none_with_fewer_than_two_numbers() -> None:
    assert _parse_ci_string("no bounds here") is None


def test_apply_transform_neg_log10_to_p() -> None:
    value = _apply_transform("neg_log10_to_p", "p_value", "3")
    assert value == pytest.approx(0.001)


def test_apply_transform_or_to_beta() -> None:
    import math

    value = _apply_transform("or_to_beta", "effect_value", "2.718281828")
    assert value == pytest.approx(1.0, abs=1e-6)
    assert value == pytest.approx(math.log(2.718281828))


def test_apply_transform_or_to_beta_rejects_non_positive() -> None:
    assert _apply_transform("or_to_beta", "effect_value", "-1") is None


def test_apply_transform_parse_ci_string_picks_correct_bound() -> None:
    assert _apply_transform("parse_ci_string", "ci_lower", "[1.2, 3.4]") == pytest.approx(1.2)
    assert _apply_transform("parse_ci_string", "ci_upper", "[1.2, 3.4]") == pytest.approx(3.4)


def test_apply_transform_identity_coerces_by_target_field_type() -> None:
    assert _apply_transform(None, "p_value", "0.05") == pytest.approx(0.05)
    assert _apply_transform(None, "n_cases", "381231") == 381231
    assert _apply_transform(None, "gene_symbol_raw", "BRCA1") == "BRCA1"


# Regression: real backman2021 data has raw "+"/"-" for effect_direction and
# "QT"/"BT" for trait_type -- passed through unnormalized, these failed
# AssociationRecord's own schema at S10 publish time for 2,087/2,406 records.
def test_coerce_field_value_normalizes_effect_direction_sign_aliases() -> None:
    assert _coerce_field_value("effect_direction", "+") == "increases"
    assert _coerce_field_value("effect_direction", "-") == "decreases"


def test_coerce_field_value_normalizes_trait_type_abbreviations() -> None:
    assert _coerce_field_value("trait_type", "QT") == "quantitative"
    assert _coerce_field_value("trait_type", "BT") == "binary"


def test_coerce_field_value_passes_through_already_canonical_enum_values() -> None:
    assert _coerce_field_value("trait_type", "binary") == "binary"
    assert _coerce_field_value("effect_direction", "increases") == "increases"
    assert _coerce_field_value("effect_type", "odds_ratio") == "odds_ratio"


def test_coerce_field_value_returns_none_for_unrecognized_enum_value() -> None:
    # Rather than writing garbage through (or guessing), an unrecognized raw
    # value for a controlled-vocabulary field becomes None -- honest, and
    # still schema-valid at publish time.
    assert _coerce_field_value("trait_type", "dMRI") is None
    assert _coerce_field_value("effect_type", "0.17 (0.13, 0.21)") is None


def test_coerce_field_value_enum_normalization_is_case_insensitive() -> None:
    assert _coerce_field_value("trait_type", "qt") == "quantitative"
    assert _coerce_field_value("trait_type", "Binary") == "binary"


def test_resolve_column_index_matches_single_row_header() -> None:
    header_rows = [["Gene", "Effect allele", "P"]]
    assert _resolve_column_index(header_rows, "Effect allele") == 1


def test_resolve_column_index_is_case_insensitive() -> None:
    header_rows = [["Gene", "Effect Allele", "P"]]
    assert _resolve_column_index(header_rows, "effect allele") == 1


def test_resolve_column_index_matches_concatenated_multi_row_header() -> None:
    # Van Hout-style merged headers.
    header_rows = [["Effect", "Effect"], ["allele", "size"]]
    assert _resolve_column_index(header_rows, "Effect allele") == 0
    assert _resolve_column_index(header_rows, "Effect size") == 1


def test_resolve_column_index_returns_none_when_not_found() -> None:
    assert _resolve_column_index([["Gene", "P"]], "Effect allele") is None


def _mapping(index: int, target: str, transform: str | None = None) -> dict:
    return {
        "source_column_index": index,
        "target_field": target,
        "transform": transform,
    }


def test_build_record_fields_applies_mappings_and_constants() -> None:
    fields = _build_record_fields(
        cells_by_col={0: "BRCA1", 1: "Breast cancer", 2: "0.05"},
        mappings=[
            _mapping(0, "gene_symbol_raw"),
            _mapping(1, "trait_raw"),
            _mapping(2, "p_value"),
        ],
        constant_fields={"cohort_name": "UK Biobank"},
        effect_allele_source="unresolvable",
        effect_allele_index=None,
    )
    assert fields["gene_symbol_raw"] == "BRCA1"
    assert fields["trait_raw"] == "Breast cancer"
    assert fields["p_value"] == pytest.approx(0.05)
    assert fields["cohort_name"] == "UK Biobank"
    assert fields["effect_allele"] is None
    assert fields["effect_direction"] == "unknown"
    assert fields["entity_type"] == "gene"


def test_build_record_fields_column_mapping_overrides_constant() -> None:
    fields = _build_record_fields(
        cells_by_col={0: "EUR-per-row"},
        mappings=[_mapping(0, "ancestry")],
        constant_fields={"ancestry": "EUR-default"},
        effect_allele_source="unresolvable",
        effect_allele_index=None,
    )
    assert fields["ancestry"] == "EUR-per-row"


def test_build_record_fields_resolves_effect_allele_from_column() -> None:
    fields = _build_record_fields(
        cells_by_col={0: "BRCA1", 5: "A"},
        mappings=[_mapping(0, "gene_symbol_raw")],
        constant_fields={},
        effect_allele_source="column",
        effect_allele_index=5,
    )
    assert fields["effect_allele"] == "A"
    assert "effect_direction" not in fields  # never forced to "unknown" when resolved


def test_build_record_fields_entity_type_defaults_to_variant_when_variant_raw_present() -> None:
    fields = _build_record_fields(
        cells_by_col={0: "1:12345:A:G"},
        mappings=[_mapping(0, "variant_raw")],
        constant_fields={},
        effect_allele_source="unresolvable",
        effect_allele_index=None,
    )
    assert fields["entity_type"] == "variant"


def test_is_complete_requires_trait_and_an_identifier() -> None:
    assert _is_complete({"trait_raw": "X", "gene_symbol_raw": "BRCA1"}) is True
    assert _is_complete({"trait_raw": "X", "variant_raw": "rs123"}) is True
    assert _is_complete({"trait_raw": "X"}) is False
    assert _is_complete({"gene_symbol_raw": "BRCA1"}) is False


def test_select_primary_contract_prefers_non_needs_review() -> None:
    a = {"needs_review": True, "overall_confidence": 0.9, "model_spec": "anthropic:x"}
    b = {"needs_review": False, "overall_confidence": 0.5, "model_spec": "openai:y"}
    assert _select_primary_contract([a, b]) is b


def test_select_primary_contract_prefers_higher_confidence_when_review_status_ties() -> None:
    a = {"needs_review": False, "overall_confidence": 0.5, "model_spec": "anthropic:x"}
    b = {"needs_review": False, "overall_confidence": 0.9, "model_spec": "openai:y"}
    assert _select_primary_contract([a, b]) is b


def test_select_primary_contract_prefers_anthropic_on_a_dead_tie() -> None:
    a = {"needs_review": False, "overall_confidence": 0.5, "model_spec": "anthropic:x"}
    b = {"needs_review": False, "overall_confidence": 0.5, "model_spec": "openai:y"}
    assert _select_primary_contract([a, b]) is a


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


def _seed_group(conn, *, run_id: str, artifact_id: str, needs_review: bool = False) -> str:
    """One contract + one member table with 3 data rows: a complete gene
    row, a complete variant row, and a row missing trait_raw (should drop).
    """
    table_id = insert_parsed_table(
        conn,
        run_id=run_id,
        artifact_id=artifact_id,
        extend_parse_run_id="pr_1",
        target="markdown",
        content=None,
        header_rows=[["Gene", "Trait", "P"]],
        row_count=3,
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
            {"row_index": 0, "col_index": 2, "value": "0.001"},
            {"row_index": 1, "col_index": 0, "value": "TP53"},
            {"row_index": 1, "col_index": 1, "value": "Lung cancer"},
            {"row_index": 1, "col_index": 2, "value": "0.02"},
            {"row_index": 2, "col_index": 0, "value": "EGFR"},
            {"row_index": 2, "col_index": 1, "value": ""},  # missing trait -> dropped
            {"row_index": 2, "col_index": 2, "value": "0.5"},
        ],
    )
    contract_id = insert_schema_contract(
        conn,
        parsed_table_id=table_id,
        model_spec="anthropic:claude-sonnet-5",
        row_entity="gene",
        constant_fields={"cohort_name": "UK Biobank"},
        effect_allele_source="unresolvable",
        effect_allele_column=None,
        unmapped_columns=[],
        interpretation_notes="test",
        overall_confidence=0.8,
        needs_review=needs_review,
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
            {
                "source_column": "P",
                "source_column_index": 2,
                "target_field": "p_value",
                "evidence": "e",
                "confidence": 0.9,
            },
        ],
    )
    insert_contract_table_members(conn, contract_id, [table_id])
    return contract_id


async def test_run_projects_complete_rows_and_drops_incomplete(db) -> None:
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
            sha256="abc123",
            storage_path="",
            retrieved_at=now_iso(),
            sheet_name="ST1",
        )
        insert_classification(
            conn,
            run_id=run_id,
            artifact_id=artifact_id,
            classification_id="assoc_gene_level",
            confidence=0.9,
            insights="",
            processor_id="p",
            processor_version="1",
            retried=False,
            needs_review=False,
        )
        _seed_group(conn, run_id=run_id, artifact_id=artifact_id)

    result = await s6_project.run(run_id)
    assert result.status == "done"
    assert result.counts["projected"] == 2
    assert result.counts["dropped_incomplete"] == 1

    with engine.begin() as conn:
        assert count_records_for_run(conn, run_id) == 2


async def test_run_is_idempotent_on_force(db) -> None:
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
            sha256="abc123",
            storage_path="",
            retrieved_at=now_iso(),
            sheet_name="ST1",
        )
        insert_classification(
            conn,
            run_id=run_id,
            artifact_id=artifact_id,
            classification_id="assoc_gene_level",
            confidence=0.9,
            insights="",
            processor_id="p",
            processor_version="1",
            retried=False,
            needs_review=False,
        )
        _seed_group(conn, run_id=run_id, artifact_id=artifact_id)

    await s6_project.run(run_id)
    await s6_project.run(run_id, force=True)

    with engine.begin() as conn:
        assert count_records_for_run(conn, run_id) == 2  # not 4


async def test_run_skips_without_force_when_records_already_exist(db) -> None:
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
            sha256="abc123",
            storage_path="",
            retrieved_at=now_iso(),
            sheet_name="ST1",
        )
        insert_classification(
            conn,
            run_id=run_id,
            artifact_id=artifact_id,
            classification_id="assoc_gene_level",
            confidence=0.9,
            insights="",
            processor_id="p",
            processor_version="1",
            retried=False,
            needs_review=False,
        )
        _seed_group(conn, run_id=run_id, artifact_id=artifact_id)

    await s6_project.run(run_id)
    result = await s6_project.run(run_id)
    assert result.status == "skipped"
