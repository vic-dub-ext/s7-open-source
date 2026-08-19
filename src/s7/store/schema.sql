-- S7 run store. One SQLite file per project, at ./data/s7.db.
-- Stage N's output is persisted here and is the only input to stage N+1.

PRAGMA foreign_keys = ON;

-- ============================================================ runs
CREATE TABLE IF NOT EXISTS runs (
    id              TEXT PRIMARY KEY,
    paper_key       TEXT NOT NULL,          -- key into corpus/papers.yaml
    doi             TEXT NOT NULL,
    pmcid           TEXT,
    pipeline_version TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending|running|done|failed|partial
    stage_reached   TEXT,                   -- e.g. "s3_classify"
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- ============================================================ S0-S1: artifacts
CREATE TABLE IF NOT EXISTS artifacts (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(id),
    kind                TEXT NOT NULL,      -- article|supplement|sheet
    file_name           TEXT NOT NULL,
    mime_type           TEXT NOT NULL,
    byte_size           INTEGER NOT NULL,
    sha256              TEXT NOT NULL,
    download_url        TEXT,
    retrieved_at        TEXT NOT NULL,
    storage_path        TEXT NOT NULL,
    parent_artifact_id  TEXT REFERENCES artifacts(id),
    sheet_name          TEXT,
    row_offset          INTEGER,
    col_offset          INTEGER,
    row_count           INTEGER,
    col_count           INTEGER,
    is_classify_sample  INTEGER NOT NULL DEFAULT 0,
    skip_reason         TEXT,
    skip_detail         TEXT
);
CREATE INDEX IF NOT EXISTS idx_artifacts_run ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_parent ON artifacts(parent_artifact_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_sha256 ON artifacts(sha256);

-- ============================================================ S2: parsed tables
CREATE TABLE IF NOT EXISTS parsed_tables (
    id                    TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL REFERENCES runs(id),
    artifact_id           TEXT NOT NULL REFERENCES artifacts(id),
    extend_parse_run_id   TEXT NOT NULL,
    target                TEXT NOT NULL,     -- markdown|spatial
    content               TEXT,              -- full text for non-table chunks (e.g. article markdown)
    header_rows_json      TEXT NOT NULL,     -- the full header block, unflattened
    row_count             INTEGER NOT NULL,
    col_count             INTEGER NOT NULL,
    raw_response_path     TEXT NOT NULL,     -- verbatim Extend response on disk
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_parsed_tables_artifact ON parsed_tables(artifact_id);
CREATE INDEX IF NOT EXISTS idx_parsed_tables_run ON parsed_tables(run_id);

CREATE TABLE IF NOT EXISTS parsed_cells (
    id               TEXT PRIMARY KEY,
    parsed_table_id  TEXT NOT NULL REFERENCES parsed_tables(id),
    row_index        INTEGER NOT NULL,
    col_index        INTEGER NOT NULL,
    value            TEXT,
    sheet_row        INTEGER,               -- original workbook coordinate, post offset-map
    sheet_col        INTEGER,
    page             INTEGER,
    bbox_x0          REAL,
    bbox_y0          REAL,
    bbox_x1          REAL,
    bbox_y1          REAL
);
CREATE INDEX IF NOT EXISTS idx_parsed_cells_table ON parsed_cells(parsed_table_id);
CREATE INDEX IF NOT EXISTS idx_parsed_cells_row ON parsed_cells(parsed_table_id, row_index);

-- ============================================================ S3: classification
CREATE TABLE IF NOT EXISTS sheet_classifications (
    id                  TEXT PRIMARY KEY,
    run_id              TEXT NOT NULL REFERENCES runs(id),
    artifact_id         TEXT NOT NULL REFERENCES artifacts(id),
    classification_id   TEXT NOT NULL,
    confidence          REAL NOT NULL,
    insights            TEXT NOT NULL,
    processor_id        TEXT NOT NULL,
    processor_version   TEXT NOT NULL,
    retried             INTEGER NOT NULL DEFAULT 0,
    needs_review        INTEGER NOT NULL DEFAULT 0,
    human_override_class TEXT,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sheet_class_artifact ON sheet_classifications(artifact_id);
CREATE INDEX IF NOT EXISTS idx_sheet_class_run ON sheet_classifications(run_id);

-- ============================================================ S4: context
CREATE TABLE IF NOT EXISTS methods_bundles (
    id                    TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL REFERENCES runs(id),
    content               TEXT NOT NULL,
    token_count           INTEGER NOT NULL,
    source_artifact_ids_json TEXT NOT NULL,
    created_at            TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_methods_bundles_run ON methods_bundles(run_id);

-- ============================================================ S5: schema contracts
CREATE TABLE IF NOT EXISTS schema_contracts (
    id                      TEXT PRIMARY KEY,
    parsed_table_id         TEXT NOT NULL REFERENCES parsed_tables(id),
    model_spec              TEXT NOT NULL,      -- "provider:model"
    row_entity               TEXT NOT NULL,      -- gene|variant|gene_variant_pair
    constant_fields_json     TEXT NOT NULL,
    effect_allele_source     TEXT NOT NULL,      -- column|constant|unresolvable
    effect_allele_column     TEXT,
    unmapped_columns_json    TEXT NOT NULL,
    interpretation_notes     TEXT NOT NULL,
    overall_confidence       REAL NOT NULL,
    needs_review             INTEGER NOT NULL DEFAULT 0,
    agreement_group_id       TEXT,               -- links the two dual-model contracts for one table
    created_at                TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_contracts_table ON schema_contracts(parsed_table_id);

CREATE TABLE IF NOT EXISTS column_mappings (
    id                    TEXT PRIMARY KEY,
    contract_id           TEXT NOT NULL REFERENCES schema_contracts(id),
    source_column         TEXT NOT NULL,
    source_column_index   INTEGER NOT NULL,
    target_field          TEXT,
    transform             TEXT,
    unit                  TEXT,
    evidence              TEXT NOT NULL,
    confidence             REAL NOT NULL,
    human_corrected         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_column_mappings_contract ON column_mappings(contract_id);

-- A schema_contract's own parsed_table_id is the *representative* fragment
-- (the one whose header was used to induce it) -- Extend's block/chunk
-- parsing splits one logical table into many small parsed_table rows
-- sharing a single header, so one contract commonly covers many fragments.
-- This is the full membership S6 projects every row from.
CREATE TABLE IF NOT EXISTS contract_table_members (
    contract_id      TEXT NOT NULL REFERENCES schema_contracts(id),
    parsed_table_id  TEXT NOT NULL REFERENCES parsed_tables(id),
    PRIMARY KEY (contract_id, parsed_table_id)
);
CREATE INDEX IF NOT EXISTS idx_contract_members_table ON contract_table_members(parsed_table_id);

-- ============================================================ S6-S9: records
CREATE TABLE IF NOT EXISTS association_records (
    record_id            TEXT PRIMARY KEY,
    run_id                TEXT NOT NULL REFERENCES runs(id),
    pipeline_version      TEXT NOT NULL,
    extracted_at          TEXT NOT NULL,

    source_doi            TEXT NOT NULL,
    source_pmcid          TEXT,
    source_file_name      TEXT NOT NULL,
    source_file_sha256    TEXT NOT NULL,
    source_sheet_name     TEXT,
    source_row_index      INTEGER NOT NULL,
    source_page           INTEGER,
    -- The parsed_table *fragment* this row came from. Necessary, not
    -- redundant with source_row_index: S5 commonly coalesces many
    -- parsed_table fragments into one contract (see s5_contract.py's
    -- _group_tables_by_header), and row_index restarts at 0 in every
    -- fragment -- so source_row_index alone can't relocate the exact
    -- source parsed_cell. S8's V2 grounding check needs this to re-read
    -- the cell it's verifying against.
    source_parsed_table_id TEXT REFERENCES parsed_tables(id),
    extend_parse_run_id   TEXT NOT NULL,
    schema_contract_id    TEXT NOT NULL REFERENCES schema_contracts(id),

    entity_type           TEXT NOT NULL,
    gene_symbol_raw       TEXT,
    ensembl_gene_id       TEXT,
    variant_raw           TEXT,
    chrom                 TEXT,
    pos_b38               INTEGER,
    ref                   TEXT,
    alt                   TEXT,
    rsid                  TEXT,

    trait_raw             TEXT NOT NULL,
    trait_label           TEXT,
    efo_id                TEXT,
    trait_type            TEXT,
    trait_units           TEXT,

    test_method           TEXT,
    test_method_raw       TEXT,
    variant_mask_raw      TEXT,
    variant_mask_class    TEXT,
    maf_threshold         REAL,

    effect_value          REAL,
    effect_type           TEXT,
    effect_allele         TEXT,
    other_allele          TEXT,
    effect_direction      TEXT,
    standard_error        REAL,
    p_value                REAL,
    ci_lower               REAL,
    ci_upper                REAL,

    cohort_name            TEXT,
    ancestry               TEXT,
    n_total                 INTEGER,
    n_cases                  INTEGER,
    n_controls                INTEGER,
    n_carriers                 INTEGER,
    analysis_role               TEXT,

    confidence             REAL NOT NULL,
    review_status           TEXT NOT NULL,

    strand_ambiguous         INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_records_run ON association_records(run_id);
CREATE INDEX IF NOT EXISTS idx_records_review_status ON association_records(review_status);
CREATE INDEX IF NOT EXISTS idx_records_gene ON association_records(gene_symbol_raw);
CREATE INDEX IF NOT EXISTS idx_records_contract ON association_records(schema_contract_id);

CREATE TABLE IF NOT EXISTS check_results (
    id            TEXT PRIMARY KEY,
    record_id     TEXT NOT NULL REFERENCES association_records(record_id),
    check_name    TEXT NOT NULL,
    status        TEXT NOT NULL,   -- pass|fail|warn|skip
    detail        TEXT NOT NULL,
    checked_by    TEXT NOT NULL,   -- "code" or "llm:<model>"
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_check_results_record ON check_results(record_id);

-- ============================================================ human review
CREATE TABLE IF NOT EXISTS human_labels (
    id                TEXT PRIMARY KEY,
    target_type       TEXT NOT NULL,   -- "record" | "artifact_classification" | "column_mapping"
    target_id         TEXT NOT NULL,
    field             TEXT,
    original_value    TEXT,
    corrected_value   TEXT,
    action            TEXT NOT NULL,   -- confirm|correct|reject
    note              TEXT,
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_human_labels_target ON human_labels(target_type, target_id);

-- ============================================================ providers
CREATE TABLE IF NOT EXISTS llm_calls (
    id               TEXT PRIMARY KEY,
    run_id           TEXT REFERENCES runs(id),
    stage            TEXT NOT NULL,
    entity_id        TEXT,
    provider         TEXT NOT NULL,
    model            TEXT NOT NULL,
    prompt_hash      TEXT NOT NULL,
    prompt           TEXT NOT NULL,
    response         TEXT NOT NULL,
    input_tokens     INTEGER,
    output_tokens    INTEGER,
    cost_usd         REAL,
    latency_ms       INTEGER,
    ok               INTEGER NOT NULL DEFAULT 1,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_run ON llm_calls(run_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_stage ON llm_calls(stage);

CREATE TABLE IF NOT EXISTS provider_calls (
    id             TEXT PRIMARY KEY,
    run_id         TEXT REFERENCES runs(id),
    stage          TEXT NOT NULL,
    provider       TEXT NOT NULL,   -- extend|ensembl|ols4|europepmc|unpaywall
    operation      TEXT NOT NULL,
    cost_credits   REAL,
    cached         INTEGER NOT NULL DEFAULT 0,
    latency_ms     INTEGER,
    ok             INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_provider_calls_run ON provider_calls(run_id);

CREATE TABLE IF NOT EXISTS ontology_cache (
    id             TEXT PRIMARY KEY,
    kind           TEXT NOT NULL,   -- gene_symbol|variant|trait
    raw_value      TEXT NOT NULL,
    resolved_json  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    UNIQUE(kind, raw_value)
);

-- Extend file uploads, cached by content hash -- re-running a paper must
-- never re-upload bytes it has already uploaded, even in a different run.
CREATE TABLE IF NOT EXISTS extend_file_uploads (
    sha256         TEXT PRIMARY KEY,
    file_id        TEXT NOT NULL,
    credits        REAL,
    uploaded_at    TEXT NOT NULL
);

-- Extend parse results, cached by content hash + config hash -- re-running
-- a paper must never re-bill Extend for a file it has already parsed with
-- the same configuration.
CREATE TABLE IF NOT EXISTS extend_parse_cache (
    cache_key       TEXT PRIMARY KEY,   -- sha256:config_hash
    sha256          TEXT NOT NULL,
    config_hash     TEXT NOT NULL,
    parse_run_id    TEXT NOT NULL,
    status          TEXT NOT NULL,      -- PROCESSED|FAILED
    raw_response_path TEXT,
    credits         REAL,
    created_at      TEXT NOT NULL
);

-- Extend classify results, cached by content hash + taxonomy config hash --
-- the final decision after any confidence-triggered retry, so a cache hit
-- never needs to know how many attempts it took. No saved Extend classifier
-- is used (see providers/extend.py's create_classify_run docstring) -- the
-- taxonomy is sent inline on every call, so the cache key is a hash of that
-- inline config rather than a classifier id/version.
CREATE TABLE IF NOT EXISTS extend_classify_cache (
    cache_key          TEXT PRIMARY KEY,  -- sha256:config_hash
    sha256             TEXT NOT NULL,
    config_hash        TEXT NOT NULL,
    classification_id  TEXT NOT NULL,
    confidence         REAL NOT NULL,
    insights           TEXT NOT NULL,
    retried            INTEGER NOT NULL DEFAULT 0,
    needs_review       INTEGER NOT NULL DEFAULT 0,
    credits            REAL,
    created_at         TEXT NOT NULL
);

-- ============================================================ events (drives the UI log stream)
CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       TEXT REFERENCES runs(id),
    stage        TEXT,
    entity_id    TEXT,
    level        TEXT NOT NULL,   -- debug|info|warn|error
    event_type   TEXT NOT NULL,
    message      TEXT NOT NULL,
    payload_json TEXT,
    ts           TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_stage ON events(run_id, stage);
