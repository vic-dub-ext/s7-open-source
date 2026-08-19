import pytest
from sqlalchemy import text

from s7.config import get_settings
from s7.providers.ontology import OntologyError
from s7.stages import s7_normalize
from s7.stages.s7_normalize import (
    _clean_search_term,
    _detect_genome_build,
    _is_near_exact_match,
    _is_strand_ambiguous,
    _render_candidates_block,
    _resolve_gene,
    _resolve_trait,
    _resolve_variant,
)
from s7.store.artifacts import insert_artifact
from s7.store.contracts import insert_schema_contract
from s7.store.db import get_engine, new_id, now_iso
from s7.store.ontology import get_cached
from s7.store.parsed import insert_parsed_table
from s7.store.records import insert_association_records, list_record_identity_fields
from s7.store.runs import create_run


def test_detect_genome_build_finds_grch37() -> None:
    assert _detect_genome_build("Coordinates are given in GRCh37.") == "37"


def test_detect_genome_build_finds_hg19() -> None:
    assert _detect_genome_build("Variants called against hg19.") == "37"


def test_detect_genome_build_finds_grch38() -> None:
    assert _detect_genome_build("We used the GRCh38 reference.") == "38"


def test_detect_genome_build_returns_none_when_unstated() -> None:
    assert _detect_genome_build("No genome build mentioned here.") is None


def test_detect_genome_build_returns_none_when_both_mentioned() -> None:
    # Ambiguous -- e.g. a methods section that discusses lifting itself.
    assert _detect_genome_build("Lifted from GRCh37 to GRCh38.") is None


def test_is_strand_ambiguous_at_pair() -> None:
    assert _is_strand_ambiguous("A", "T") is True
    assert _is_strand_ambiguous("T", "A") is True


def test_is_strand_ambiguous_cg_pair() -> None:
    assert _is_strand_ambiguous("C", "G") is True


def test_is_strand_ambiguous_false_for_non_palindromic_pair() -> None:
    assert _is_strand_ambiguous("A", "G") is False


def test_is_strand_ambiguous_false_when_missing() -> None:
    assert _is_strand_ambiguous(None, "G") is False
    assert _is_strand_ambiguous("A", None) is False


def test_is_strand_ambiguous_false_for_multi_base_alleles() -> None:
    assert _is_strand_ambiguous("AT", "T") is False


def test_is_near_exact_match_on_label() -> None:
    candidate = {"label": "Major depressive disorder", "exact_synonyms": []}
    assert _is_near_exact_match("major depressive disorder", candidate) is True


def test_is_near_exact_match_on_synonym() -> None:
    candidate = {"label": "treatment resistant depression", "exact_synonyms": ["TRD"]}
    assert _is_near_exact_match("TRD", candidate) is True


def test_is_near_exact_match_false_for_unrelated_string() -> None:
    candidate = {"label": "Major depressive disorder", "exact_synonyms": []}
    assert _is_near_exact_match("Vitamin D deficiency", candidate) is False


def test_render_candidates_block_includes_description() -> None:
    block = _render_candidates_block(
        [{"obo_id": "EFO:123", "ontology": "efo", "label": "X", "description": "a thing"}]
    )
    assert "EFO:123" in block
    assert "a thing" in block


def test_render_candidates_block_omits_missing_description() -> None:
    block = _render_candidates_block([{"obo_id": "EFO:123", "ontology": "efo", "label": "X"}])
    assert block == "- EFO:123 (EFO): X"


def test_clean_search_term_strips_trailing_field_code() -> None:
    # Regression: found by hand -- OLS4 returns zero candidates for
    # "Eosinophil count (30150)" but ten good ones for "Eosinophil count".
    assert _clean_search_term("Eosinophil count (30150)") == "Eosinophil count"


def test_clean_search_term_strips_leading_icd10_code() -> None:
    assert (
        _clean_search_term("ICD10 I71: Aortic aneurysm and dissection")
        == "Aortic aneurysm and dissection"
    )


def test_clean_search_term_replaces_underscores_with_spaces() -> None:
    assert _clean_search_term("Leg_fat_free_mass_right") == "Leg fat free mass right"


def test_clean_search_term_leaves_clean_strings_unchanged() -> None:
    assert _clean_search_term("Vitamin D") == "Vitamin D"


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


class _ErroringOntologyClient:
    """Every lookup raises OntologyError -- simulates a transient API blip,
    not a confirmed "not found."
    """

    async def resolve_gene_symbol(self, symbol):
        raise OntologyError("simulated transient failure")

    async def resolve_rsid(self, rsid):
        raise OntologyError("simulated transient failure")

    async def liftover_37_to_38(self, chrom, pos):
        raise OntologyError("simulated transient failure")

    async def search_trait_candidates(self, term):
        raise OntologyError("simulated transient failure")


# Regression: a transient OntologyError was being cached identically to a
# confirmed "not found," permanently poisoning ontology_cache for every
# future run (any paper, forever) that tests the same symbol/rsid/trait.
# Found live against a real paper, where an OLS4 blip cached "Schizophrenia"
# as unresolved even though the API worked moments later.
async def test_resolve_gene_does_not_cache_on_transient_error(db) -> None:
    engine = db
    counts = {"genes_resolved": 0, "genes_unresolved": 0}
    with engine.begin() as conn:
        result = await _resolve_gene(conn, _ErroringOntologyClient(), "BRCA1", counts)
        assert result is None
        assert counts["genes_unresolved"] == 1
        assert get_cached(conn, kind="gene_symbol", raw_value="BRCA1") is None


async def test_resolve_variant_does_not_cache_rsid_lookup_on_transient_error(db) -> None:
    engine = db
    counts = {
        "variants_resolved": 0,
        "variants_unresolved": 0,
        "variants_unparseable": 0,
        "variants_lifted": 0,
    }
    with engine.begin() as conn:
        result = await _resolve_variant(
            conn, _ErroringOntologyClient(), "rs123", genome_build=None, counts=counts
        )
        assert result == {"rsid": "rs123"}
        assert counts["variants_unresolved"] == 1
        assert get_cached(conn, kind="variant", raw_value="rs123") is None


async def test_resolve_variant_does_not_cache_unlifted_position_on_liftover_error(db) -> None:
    # The dangerous case: a failed liftover falls back to the unlifted
    # (GRCh37) position for THIS run's record -- but that value must never
    # be cached as if it were a confirmed GRCh38 "pos_b38", or a future run
    # would silently trust the wrong coordinate.
    engine = db
    counts = {
        "variants_resolved": 0,
        "variants_unresolved": 0,
        "variants_unparseable": 0,
        "variants_lifted": 0,
    }
    with engine.begin() as conn:
        result = await _resolve_variant(
            conn, _ErroringOntologyClient(), "1:12345:A:T", genome_build="37", counts=counts
        )
        assert result["pos_b38"] == 12345  # unlifted fallback, used for this run only
        assert counts["variants_lifted"] == 0
        assert get_cached(conn, kind="variant", raw_value="1:12345:A:T") is None


async def test_resolve_trait_does_not_cache_on_transient_error(db) -> None:
    engine = db
    counts = {"traits_matched_exact": 0, "traits_matched_llm": 0, "traits_needs_review": 0}
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1/test")
        result = await _resolve_trait(
            conn, _ErroringOntologyClient(), get_settings(), run_id, "Schizophrenia", counts
        )
        assert result is None
        assert counts["traits_needs_review"] == 1
        assert get_cached(conn, kind="trait", raw_value="Schizophrenia") is None


async def _fake_resolve_gene(conn, client, symbol, counts):
    counts["genes_resolved"] += 1
    return f"ENSG_FAKE_{symbol}"


async def _fake_resolve_variant(conn, client, raw, *, genome_build, counts):
    counts["variants_resolved"] += 1
    return {"chrom": "1", "pos_b38": 100, "ref": "A", "alt": "T"}


async def _fake_resolve_trait(conn, client, settings, run_id, raw, counts):
    counts["traits_matched_exact"] += 1
    return f"EFO_FAKE_{raw}"


def _seed_records(conn, run_id: str, artifact_id: str) -> None:
    table_id = insert_parsed_table(
        conn,
        run_id=run_id,
        artifact_id=artifact_id,
        extend_parse_run_id="pr_1",
        target="markdown",
        content=None,
        header_rows=[["Gene", "Variant", "Trait"]],
        row_count=1,
        col_count=3,
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
        overall_confidence=0.8,
        needs_review=False,
        agreement_group_id=new_id(),
    )
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
                "extend_parse_run_id": "pr_1",
                "schema_contract_id": contract_id,
                "entity_type": "gene",
                "gene_symbol_raw": "BRCA1",
                "variant_raw": "1:12345:A:T",
                "trait_raw": "Breast cancer",
                "confidence": 0.8,
                "review_status": "needs_review",
            }
        ],
    )


async def test_run_updates_records_with_resolved_identifiers(db, monkeypatch) -> None:
    monkeypatch.setattr(s7_normalize, "_resolve_gene", _fake_resolve_gene)
    monkeypatch.setattr(s7_normalize, "_resolve_variant", _fake_resolve_variant)
    monkeypatch.setattr(s7_normalize, "_resolve_trait", _fake_resolve_trait)

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
        _seed_records(conn, run_id, artifact_id)

    result = await s7_normalize.run(run_id)
    assert result.status == "done"
    assert result.counts["records_updated"] == 1

    with engine.begin() as conn:
        row = (
            conn.execute(text("SELECT * FROM association_records WHERE record_id = 'rec-1'"))
            .mappings()
            .first()
        )
    assert row["ensembl_gene_id"] == "ENSG_FAKE_BRCA1"
    assert row["chrom"] == "1"
    assert row["ref"] == "A"
    assert row["alt"] == "T"
    assert row["strand_ambiguous"] == 1  # A/T is a strand-ambiguous pair
    assert row["efo_id"] == "EFO_FAKE_Breast cancer"


async def test_run_skips_without_force_once_finished(db, monkeypatch) -> None:
    monkeypatch.setattr(s7_normalize, "_resolve_gene", _fake_resolve_gene)
    monkeypatch.setattr(s7_normalize, "_resolve_variant", _fake_resolve_variant)
    monkeypatch.setattr(s7_normalize, "_resolve_trait", _fake_resolve_trait)

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
        _seed_records(conn, run_id, artifact_id)

    await s7_normalize.run(run_id)
    result = await s7_normalize.run(run_id)
    assert result.status == "skipped"


async def test_run_is_idempotent_on_force(db, monkeypatch) -> None:
    monkeypatch.setattr(s7_normalize, "_resolve_gene", _fake_resolve_gene)
    monkeypatch.setattr(s7_normalize, "_resolve_variant", _fake_resolve_variant)
    monkeypatch.setattr(s7_normalize, "_resolve_trait", _fake_resolve_trait)

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
        _seed_records(conn, run_id, artifact_id)

    await s7_normalize.run(run_id)
    result = await s7_normalize.run(run_id, force=True)
    assert result.status == "done"
    assert result.counts["records_updated"] == 1

    with engine.begin() as conn:
        assert len(list_record_identity_fields(conn, run_id)) == 1
