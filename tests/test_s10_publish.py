import json
from typing import Literal

import pyarrow.parquet as pq
import pytest
from sqlalchemy import text

from s7.config import get_settings
from s7.stages import s10_publish
from s7.stages.s10_publish import _format_type, _record_to_row, generate_data_dictionary
from s7.store.artifacts import insert_artifact
from s7.store.checks import insert_check_results
from s7.store.classifications import insert_classification
from s7.store.contracts import insert_column_mappings, insert_schema_contract
from s7.store.db import get_engine, new_id, now_iso
from s7.store.parsed import insert_parsed_table
from s7.store.records import insert_association_records, update_normalized_fields
from s7.store.runs import create_run


def test_format_type_renders_optional_literal() -> None:
    rendered = _format_type(Literal["increases", "decreases", "unknown"] | None)
    assert "increases" in rendered
    assert rendered.endswith("null")


def test_format_type_renders_plain_str() -> None:
    assert _format_type(str) == "str"


def test_generate_data_dictionary_lists_every_field() -> None:
    from s7.models.record import AssociationRecord

    doc = generate_data_dictionary()
    for name in AssociationRecord.model_fields:
        assert f"`{name}`" in doc
    # A required field and an optional one both render their required column.
    assert "| `record_id` |" in doc


def test_record_to_row_drops_run_id_and_attaches_checks() -> None:
    row = _record_to_row(
        {
            "record_id": "rec-1",
            "run_id": "should-be-dropped",
            "pipeline_version": "0.1.0",
            "extracted_at": now_iso(),
            "source_doi": "10.1/test",
            "source_file_name": "t.xlsx",
            "source_file_sha256": "abc",
            "source_row_index": 0,
            "extend_parse_run_id": "pr_1",
            "schema_contract_id": "contract-1",
            "entity_type": "gene",
            "trait_raw": "Breast cancer",
            "confidence": 0.9,
            "review_status": "auto_pass",
            "strand_ambiguous": 1,
        },
        [{"check_name": "v1_arithmetic", "status": "pass", "detail": "ok", "checked_by": "code"}],
    )
    assert "run_id" not in row
    assert row["strand_ambiguous"] is True
    assert row["check_results"] == [
        {"check_name": "v1_arithmetic", "status": "pass", "detail": "ok", "checked_by": "code"}
    ]


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


def _seed_run_with_three_records(conn) -> str:
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
        confidence=0.95,
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
        row_count=3,
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
    records = [
        {
            "record_id": f"rec-{status}",
            "run_id": run_id,
            "pipeline_version": "0.1.0",
            "extracted_at": now_iso(),
            "source_doi": "10.1/test",
            "source_file_name": "t.xlsx",
            "source_file_sha256": "abc",
            "source_row_index": i,
            "source_parsed_table_id": table_id,
            "extend_parse_run_id": "pr_1",
            "schema_contract_id": contract_id,
            "entity_type": "gene",
            "gene_symbol_raw": "BRCA1",
            "trait_raw": "Breast cancer",
            "confidence": 0.9 if status == "auto_pass" else 0.2,
            "review_status": status,
        }
        for i, status in enumerate(["auto_pass", "needs_review", "rejected"])
    ]
    insert_association_records(conn, records)
    insert_check_results(
        conn,
        [
            {
                "record_id": "rec-auto_pass",
                "check_name": "v1_arithmetic",
                "status": "pass",
                "detail": "ok",
                "checked_by": "code",
            }
        ],
    )
    return run_id


async def test_run_writes_main_and_quarantine_datasets_and_excludes_rejected(db) -> None:
    engine = db
    settings = get_settings()
    with engine.begin() as conn:
        run_id = _seed_run_with_three_records(conn)

    result = await s10_publish.run(run_id)
    assert result.status == "done"
    assert result.counts["records_published_main"] == 1
    assert result.counts["records_published_quarantine"] == 1
    assert result.counts["records_excluded_rejected"] == 1

    main_table = pq.read_table(str(settings.parquet_dir))
    assert main_table.num_rows == 1
    assert main_table.column("record_id").to_pylist() == ["rec-auto_pass"]

    quarantine_table = pq.read_table(str(settings.quarantine_dir))
    assert quarantine_table.num_rows == 1
    assert quarantine_table.column("record_id").to_pylist() == ["rec-needs_review"]

    # rejected record appears in neither dataset.
    all_ids = set(main_table.column("record_id").to_pylist()) | set(
        quarantine_table.column("record_id").to_pylist()
    )
    assert "rec-rejected" not in all_ids


async def test_run_writes_data_dictionary_and_coverage_report(db) -> None:
    engine = db
    settings = get_settings()
    with engine.begin() as conn:
        run_id = _seed_run_with_three_records(conn)

    await s10_publish.run(run_id)

    assert settings.data_dictionary_path.exists()
    assert "record_id" in settings.data_dictionary_path.read_text()

    report = json.loads(settings.coverage_report_path.read_text())
    entry = report["papers"]["10.1/test"]
    assert entry["records_projected"] == 3
    assert entry["published_main_dataset"] == 1
    assert entry["published_quarantine"] == 1
    assert entry["excluded_rejected"] == 1
    assert entry["run_id"] == run_id


async def test_run_excludes_a_record_that_fails_schema_validation_without_crashing(db) -> None:
    # Regression: a real upstream bug surfaced a record whose effect_type
    # held a garbage string (a mis-mapped "effect (CI)" column, not one of
    # AssociationRecord's controlled values) -- one bad row must not take
    # down the whole publish.
    engine = db
    with engine.begin() as conn:
        run_id = _seed_run_with_three_records(conn)
        conn.execute(
            text("UPDATE association_records SET effect_type = :bad WHERE record_id = :id"),
            {"bad": "0.17 (0.13, 0.21)", "id": "rec-auto_pass"},
        )

    result = await s10_publish.run(run_id)
    assert result.status == "done"
    assert result.counts["records_excluded_failed_validation"] == 1
    # The corrupted record was the only auto_pass row, so main is now empty.
    assert result.counts["records_published_main"] == 0

    report = json.loads(get_settings().coverage_report_path.read_text())
    assert report["papers"]["10.1/test"]["excluded_failed_validation"] == 1


async def test_run_skips_without_force_once_finished(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id = _seed_run_with_three_records(conn)

    await s10_publish.run(run_id)
    result = await s10_publish.run(run_id)
    assert result.status == "skipped"


async def test_run_is_idempotent_on_force(db) -> None:
    engine = db
    settings = get_settings()
    with engine.begin() as conn:
        run_id = _seed_run_with_three_records(conn)

    await s10_publish.run(run_id)
    result = await s10_publish.run(run_id, force=True)
    assert result.status == "done"

    # Re-publishing doesn't duplicate rows in the partition.
    main_table = pq.read_table(str(settings.parquet_dir))
    assert main_table.num_rows == 1


async def test_two_papers_with_different_null_columns_read_as_one_dataset(db) -> None:
    # Regression: pyarrow infers a column's type from whatever's in one
    # write's batch of rows. A field entirely None in one paper's records
    # (e.g. chrom/pos_b38/ref/alt, gene-level-only papers never populate
    # these) got inferred as pyarrow's `null` type, while the same field
    # had real string/int data in another paper's batch -- reading the two
    # partitions together then failed with "Unsupported cast from string
    # to null." Found live, publishing a second real paper alongside the
    # first. The fix is an explicit schema derived once from
    # AssociationRecord, used for every write regardless of what's null in
    # that particular batch.
    engine = db
    settings = get_settings()
    with engine.begin() as conn:
        gene_level_run = _seed_run_with_three_records(conn)  # chrom/pos_b38/etc all None

        variant_run = create_run(conn, paper_key="test2", doi="10.2/test")
        artifact_id = insert_artifact(
            conn,
            run_id=variant_run,
            kind="sheet",
            file_name="t2.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            byte_size=10,
            sha256="def",
            storage_path="",
            retrieved_at=now_iso(),
            sheet_name="ST1",
        )
        table_id = insert_parsed_table(
            conn,
            run_id=variant_run,
            artifact_id=artifact_id,
            extend_parse_run_id="pr_2",
            target="markdown",
            content=None,
            header_rows=[["Variant", "Trait"]],
            row_count=1,
            col_count=2,
            raw_response_path="",
            created_at=now_iso(),
        )
        contract_id = insert_schema_contract(
            conn,
            parsed_table_id=table_id,
            model_spec="anthropic:claude-sonnet-5",
            row_entity="variant",
            constant_fields={},
            effect_allele_source="unresolvable",
            effect_allele_column=None,
            unmapped_columns=[],
            interpretation_notes="test",
            overall_confidence=0.9,
            needs_review=False,
            agreement_group_id=new_id(),
        )
        insert_association_records(
            conn,
            [
                {
                    "record_id": "rec-variant",
                    "run_id": variant_run,
                    "pipeline_version": "0.1.0",
                    "extracted_at": now_iso(),
                    "source_doi": "10.2/test",
                    "source_file_name": "t2.xlsx",
                    "source_file_sha256": "def",
                    "source_row_index": 0,
                    "source_parsed_table_id": table_id,
                    "extend_parse_run_id": "pr_2",
                    "schema_contract_id": contract_id,
                    "entity_type": "variant",
                    "variant_raw": "1:12345:A:T",
                    "trait_raw": "Breast cancer",
                    "confidence": 0.9,
                    "review_status": "auto_pass",
                }
            ],
        )
        # chrom/pos_b38/ref/alt are only ever written by S7's write-back
        # (update_normalized_fields), never by the initial insert -- match
        # the real pipeline's write path, not just the column's existence.
        update_normalized_fields(
            conn,
            [
                {
                    "record_id": "rec-variant",
                    "chrom": "1",
                    "pos_b38": 12345,
                    "ref": "A",
                    "alt": "T",
                }
            ],
        )

    await s10_publish.run(gene_level_run)
    await s10_publish.run(variant_run)

    # This is the read that used to raise ArrowNotImplementedError.
    combined = pq.read_table(str(settings.parquet_dir))
    assert combined.num_rows == 2
    by_id = {
        row["record_id"]: row for row in combined.select(["record_id", "chrom"]).to_pylist()
    }
    assert by_id["rec-auto_pass"]["chrom"] is None
    assert by_id["rec-variant"]["chrom"] == "1"
