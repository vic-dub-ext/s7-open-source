from datetime import UTC, datetime

from s7.models.record import AssociationRecord, record_id_for


def _minimal_record(**overrides: object) -> AssociationRecord:
    defaults: dict[str, object] = dict(
        record_id=record_id_for(
            source_file_sha256="abc123",
            parsed_table_id="table-1",
            source_row_index=0,
            entity_key="SLC2A1",
            trait_raw="depression",
        ),
        pipeline_version="0.1.0",
        extracted_at=datetime.now(UTC),
        source_doi="10.1038/s41467-024-45774-2",
        source_file_name="Supplementary_Table_S7.xlsx",
        source_file_sha256="abc123",
        source_row_index=0,
        extend_parse_run_id="run-1",
        schema_contract_id="contract-1",
        entity_type="gene",
        gene_symbol_raw="SLC2A1",
        trait_raw="depression",
        confidence=0.9,
        review_status="auto_pass",
    )
    defaults.update(overrides)
    return AssociationRecord(**defaults)  # type: ignore[arg-type]


def test_minimal_record_constructs() -> None:
    record = _minimal_record()
    assert record.entity_type == "gene"
    assert record.check_results == []


def test_record_id_is_deterministic() -> None:
    kwargs = dict(
        source_file_sha256="abc123",
        parsed_table_id="table-1",
        source_row_index=0,
        entity_key="SLC2A1",
        trait_raw="depression",
    )
    assert record_id_for(**kwargs) == record_id_for(**kwargs)


def test_record_id_differs_on_row_index() -> None:
    a = record_id_for(
        source_file_sha256="abc123",
        parsed_table_id="table-1",
        source_row_index=0,
        entity_key="SLC2A1",
        trait_raw="depression",
    )
    b = record_id_for(
        source_file_sha256="abc123",
        parsed_table_id="table-1",
        source_row_index=1,
        entity_key="SLC2A1",
        trait_raw="depression",
    )
    assert a != b


def test_record_id_differs_on_parsed_table_id() -> None:
    # Regression: found by hand against real data -- Extend's block/chunk
    # parsing gives every fragment its own 0-based row_index (and even its
    # own "sheet_row"), so two genuinely different rows in two different
    # fragments of the same S5-coalesced group can share a row_index. The
    # fragment id must be part of the hash or they collide into one record.
    a = record_id_for(
        source_file_sha256="abc123",
        parsed_table_id="table-1",
        source_row_index=0,
        entity_key="SLC2A1",
        trait_raw="depression",
    )
    b = record_id_for(
        source_file_sha256="abc123",
        parsed_table_id="table-2",
        source_row_index=0,
        entity_key="SLC2A1",
        trait_raw="depression",
    )
    assert a != b
