import json
from datetime import UTC, datetime

import pytest

from s7.models.contract import ColumnMapping, ContractInduction
from s7.providers.llm import LLMCallRecord
from s7.stages import s5_contract
from s7.stages.s5_contract import (
    _group_tables_by_header,
    _mappings_disagree,
    _render_header_block,
)
from s7.store.artifacts import insert_artifact
from s7.store.classifications import insert_classification
from s7.store.db import get_engine, now_iso
from s7.store.llm_calls import list_llm_calls_for_entity
from s7.store.parsed import insert_parsed_cells, insert_parsed_table
from s7.store.runs import create_run


def _table(id_: str, header_rows: list[list[str]] | None, row_count: int = 5) -> dict:
    return {
        "id": id_,
        "header_rows_json": json.dumps(header_rows or []),
        "row_count": row_count,
        "col_count": len(header_rows[0]) if header_rows else 0,
    }


HEADER_A = [["Gene", "Trait", "P"]]
HEADER_B = [["Variant", "Chr", "Pos", "P"]]


def test_group_tables_by_header_merges_consecutive_fragments_sharing_a_header() -> None:
    tables = [_table("t1", HEADER_A), _table("t2", HEADER_A), _table("t3", HEADER_A)]
    groups = _group_tables_by_header(tables)
    assert len(groups) == 1
    assert [t["id"] for t in groups[0]] == ["t1", "t2", "t3"]


def test_group_tables_by_header_splits_on_a_different_header() -> None:
    tables = [_table("t1", HEADER_A), _table("t2", HEADER_A), _table("t3", HEADER_B)]
    groups = _group_tables_by_header(tables)
    assert len(groups) == 2
    assert [t["id"] for t in groups[0]] == ["t1", "t2"]
    assert [t["id"] for t in groups[1]] == ["t3"]


def test_group_tables_by_header_treats_headerless_fragment_as_continuation() -> None:
    tables = [_table("t1", HEADER_A), _table("t2", None), _table("t3", None)]
    groups = _group_tables_by_header(tables)
    assert len(groups) == 1
    assert [t["id"] for t in groups[0]] == ["t1", "t2", "t3"]


def test_group_tables_by_header_starts_headerless_group_when_none_precedes_it() -> None:
    tables = [_table("t1", None), _table("t2", None), _table("t3", HEADER_A)]
    groups = _group_tables_by_header(tables)
    assert len(groups) == 2
    assert [t["id"] for t in groups[0]] == ["t1", "t2"]
    assert [t["id"] for t in groups[1]] == ["t3"]


def test_group_tables_by_header_resumes_first_header_after_a_different_one() -> None:
    # A repeated header returns to its own group only by adjacency -- if
    # header A appears, then B, then A again, the second A starts a THIRD
    # group rather than rejoining the first.
    tables = [_table("t1", HEADER_A), _table("t2", HEADER_B), _table("t3", HEADER_A)]
    groups = _group_tables_by_header(tables)
    assert len(groups) == 3


def test_render_header_block_pads_short_rows_to_width() -> None:
    text = _render_header_block([["A", "B"]], width=4)
    assert text == "A | B |  | "


def test_render_header_block_handles_no_header() -> None:
    assert _render_header_block([], width=3) == "(no header captured)"


def _induction(
    *, effect_allele_source="unresolvable", mappings: list[ColumnMapping] | None = None
) -> ContractInduction:
    return ContractInduction(
        row_entity="gene",
        constant_fields=[],
        column_mappings=mappings or [],
        effect_allele_source=effect_allele_source,
        unmapped_columns=[],
        interpretation_notes="",
        overall_confidence=0.5,
    )


def _mapping(index: int, target_field: str, column: str = "col") -> ColumnMapping:
    return ColumnMapping(
        source_column=column,
        source_column_index=index,
        target_field=target_field,
        evidence="e",
        confidence=0.5,
    )


def test_mappings_disagree_on_different_effect_allele_source() -> None:
    a = _induction(effect_allele_source="unresolvable")
    b = _induction(effect_allele_source="column")
    assert _mappings_disagree(a, b) is True


def test_mappings_disagree_on_different_target_field_for_same_column() -> None:
    a = _induction(mappings=[_mapping(0, "p_value", "P")])
    b = _induction(mappings=[_mapping(0, "effect_value", "P")])
    assert _mappings_disagree(a, b) is True


def test_mappings_agree_when_same_column_gets_same_multi_target_set() -> None:
    # A single source column can legitimately map to multiple target fields
    # (e.g. a combined "Effect (95% CI)" column). The comparison must treat
    # this as a set per column index, not silently keep only the last one.
    a = _induction(mappings=[_mapping(2, "ci_lower", "CI"), _mapping(2, "ci_upper", "CI")])
    b = _induction(mappings=[_mapping(2, "ci_upper", "CI"), _mapping(2, "ci_lower", "CI")])
    assert _mappings_disagree(a, b) is False


def test_mappings_disagree_when_one_side_drops_a_multi_target_mapping() -> None:
    a = _induction(mappings=[_mapping(2, "ci_lower", "CI"), _mapping(2, "ci_upper", "CI")])
    b = _induction(mappings=[_mapping(2, "ci_lower", "CI")])
    assert _mappings_disagree(a, b) is True


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


async def _fake_induce_one(*, settings, model, system, user, entity_id):
    """Stands in for a real provider call so this test never hits the
    network -- returns a trivially-valid induction for either provider.
    """
    induction = _induction(mappings=[_mapping(0, "gene_symbol_raw", "Gene")])
    record = LLMCallRecord(
        model_spec=model,
        prompt_hash="hash",
        prompt=user,
        response="{}",
        input_tokens=10,
        output_tokens=5,
        cost_usd=0.001,
        latency_ms=1,
        stage=s5_contract.STAGE,
        entity_id=entity_id,
        ok=True,
        created_at=datetime.now(UTC),
    )
    return induction, record


async def test_run_on_force_does_not_accumulate_stale_llm_calls(db, monkeypatch) -> None:
    """Regression test: re-running with --force must clear the previous
    run's llm_calls, not just its contracts -- found by hand against
    backman2021, where a single table ended up with 4 llm_calls (2 stale +
    2 fresh) after re-running.
    """
    monkeypatch.setattr(s5_contract, "_induce_one", _fake_induce_one)
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
        table_id = insert_parsed_table(
            conn,
            run_id=run_id,
            artifact_id=artifact_id,
            extend_parse_run_id="pr_1",
            target="markdown",
            content=None,
            header_rows=[["Gene", "P"]],
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
                {"row_index": 0, "col_index": 1, "value": "1e-8"},
            ],
        )

    result1 = await s5_contract.run(run_id, force=True)
    assert result1.status == "done"
    assert result1.counts["contracts_induced"] == 2
    with engine.begin() as conn:
        assert len(list_llm_calls_for_entity(conn, table_id)) == 2

    result2 = await s5_contract.run(run_id, force=True)
    assert result2.counts["contracts_induced"] == 2
    with engine.begin() as conn:
        # Not 4 -- this is the regression check.
        assert len(list_llm_calls_for_entity(conn, table_id)) == 2
