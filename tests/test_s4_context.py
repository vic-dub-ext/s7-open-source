import pytest

from s7.stages import s4_context
from s7.stages.s4_context import (
    _extract_jats_sections,
    _fit_budget,
    _render_decoder_artifact,
    _render_parsed_table,
    estimate_tokens,
)
from s7.store.artifacts import insert_artifact
from s7.store.db import get_engine, now_iso
from s7.store.parsed import insert_parsed_cells, insert_parsed_table
from s7.store.runs import create_run

JATS_SAMPLE = b"""<?xml version="1.0"?>
<article>
<body>
<sec><title>Main</title><p>Some intro text.</p></sec>
<sec><title>Methods</title>
  <p>Overall methods intro.</p>
  <sec><title>Sample preparation</title><p>We prepared samples.</p></sec>
  <sec><title>Genetic association analyses</title><p>We ran REGENIE for associations.</p></sec>
</sec>
<sec><title>Supplementary information</title><p>Supplementary Tables 1-23 described here.</p></sec>
</body>
</article>
"""


def test_extract_jats_sections_finds_methods_and_stat_subsection() -> None:
    sections = _extract_jats_sections(JATS_SAMPLE)
    assert sections is not None
    assert "Overall methods intro" in sections["methods"]
    assert "We ran REGENIE" in sections["stat_subsection"]
    assert "Supplementary Tables 1-23" in sections["table_captions"]


def test_extract_jats_sections_returns_none_for_malformed_xml() -> None:
    assert _extract_jats_sections(b"<not><valid xml") is None


def test_extract_jats_sections_returns_none_without_methods_section() -> None:
    xml = b"<article><body><sec><title>Main</title><p>Just intro.</p></sec></body></article>"
    assert _extract_jats_sections(xml) is None


def test_render_parsed_table_handles_headers_and_sparse_cells() -> None:
    header_rows = [["Gene", "P"]]
    cells = [
        {"row_index": 0, "col_index": 0, "value": "BRCA1"},
        {"row_index": 0, "col_index": 1, "value": "1e-8"},
        {"row_index": 1, "col_index": 0, "value": "TP53"},
        # row 1 col 1 missing -- must not crash, should render blank
    ]
    text = _render_parsed_table(header_rows, cells)
    lines = text.splitlines()
    assert lines[0] == "Gene | P"
    assert lines[1] == "---"
    assert "BRCA1 | 1e-8" in text
    assert "TP53 | " in text


def test_fit_budget_includes_segments_in_priority_order_within_budget(monkeypatch) -> None:
    monkeypatch.setattr(s4_context, "CHAR_BUDGET", 100)
    segments = [
        ("A", "x" * 40),
        ("B", "y" * 40),
        ("C", "z" * 40),
    ]
    result = _fit_budget(segments)
    assert "## A" in result
    assert "## B" in result
    # C should be dropped or truncated since budget is exhausted after A+B+headers
    assert "z" * 40 not in result


def test_fit_budget_skips_empty_segments() -> None:
    result = _fit_budget([("Empty", ""), ("Real", "content here")])
    assert "Empty" not in result
    assert "content here" in result


def test_estimate_tokens_is_roughly_chars_over_four() -> None:
    assert estimate_tokens("a" * 400) == 100


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


def test_render_decoder_artifact_caps_rows_and_notes_omission(db, monkeypatch) -> None:
    monkeypatch.setattr(s4_context, "MAX_DECODER_ROWS", 3)
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1/test")
        artifact_id = insert_artifact(
            conn,
            run_id=run_id,
            kind="sheet",
            file_name="decoder.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            byte_size=10,
            sha256="abc",
            storage_path="",
            retrieved_at=now_iso(),
            sheet_name="Decoder",
        )
        table_id = insert_parsed_table(
            conn,
            run_id=run_id,
            artifact_id=artifact_id,
            extend_parse_run_id="pr_1",
            target="markdown",
            content=None,
            header_rows=[["label"]],
            row_count=5,
            col_count=1,
            raw_response_path="",
            created_at=now_iso(),
        )
        insert_parsed_cells(
            conn,
            table_id,
            [{"row_index": i, "col_index": 0, "value": f"row{i}"} for i in range(5)],
        )

    artifact = {"artifact_id": artifact_id, "file_name": "decoder.xlsx", "sheet_name": "Decoder"}
    with engine.begin() as conn:
        text, total_rows = _render_decoder_artifact(conn, artifact)

    assert total_rows == 5
    assert "row0" in text
    assert "row2" in text
    assert "row4" not in text  # capped at 3 rows
    assert "2 more rows omitted" in text
