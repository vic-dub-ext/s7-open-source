# S7 Data Dictionary

Generated from `s7.models.record.AssociationRecord` -- do not hand-edit; edit the field definitions and re-run `s7 stage s10_publish` instead.

| Field | Type | Required | Description |
|---|---|---|---|
| `record_id` | str | yes | Stable uuid5 of source file + fragment + row + entity + trait (see record_id_for). Re-running the pipeline on the same input reproduces the same record_id. |
| `pipeline_version` | str | yes | s7 package version that produced this record. |
| `extracted_at` | datetime | yes | When this record was projected (S6), UTC. |
| `source_doi` | str | yes | DOI of the paper this result came from. |
| `source_pmcid` | str \| null | no | PubMed Central ID, if known. |
| `source_file_name` | str | yes | Original supplementary file name. |
| `source_file_sha256` | str | yes | SHA-256 of the source file, for content-addressed provenance. |
| `source_sheet_name` | str \| null | no | Workbook sheet name this row came from. None for PDF-sourced tables. |
| `source_row_index` | int | yes | 0-based row index within the parsed table fragment (source_parsed_table_id). |
| `source_page` | int \| null | no | PDF page number, for PDF-sourced tables. |
| `source_parsed_table_id` | str \| null | no | The exact parsed_table fragment this row came from -- necessary because a row_index restarts at 0 in every fragment (see record_id_for's docstring). |
| `extend_parse_run_id` | str | yes | Extend's own run id for the S2 parse call. |
| `schema_contract_id` | str | yes | The S5-induced schema contract used to project this row. |
| `entity_type` | 'gene' \| 'variant' | yes | Whether this row's test unit is a gene (burden/collapsing) or a single variant. |
| `gene_symbol_raw` | str \| null | no | Gene symbol exactly as printed in the source. |
| `ensembl_gene_id` | str \| null | no | Ensembl gene ID resolved from gene_symbol_raw (S7). |
| `variant_raw` | str \| null | no | Variant identifier exactly as printed in the source. |
| `chrom` | str \| null | no | Chromosome, GRCh38. |
| `pos_b38` | int \| null | no | Position, lifted to GRCh38 if needed. |
| `ref` | str \| null | no | Reference allele. |
| `alt` | str \| null | no | Alternate allele. |
| `rsid` | str \| null | no | dbSNP rsID, resolved or as printed. |
| `trait_raw` | str | yes | Trait/phenotype label exactly as printed in the source. |
| `trait_label` | str \| null | no | Normalized trait label (S7 ontology resolution). |
| `efo_id` | str \| null | no | Experimental Factor Ontology ID resolved from trait_raw (S7). |
| `trait_type` | 'binary' \| 'quantitative' \| null | no | Whether the trait is a binary (case/control) or quantitative measure. |
| `trait_units` | str \| null | no | Units for a quantitative trait, e.g. "mg/dL", "SD". None for binary. |
| `test_method` | 'burden' \| 'collapsing' \| 'single_variant' \| 'skat' \| 'skat_o' \| 'conditional' \| 'leave_one_out' \| 'meta_analysis' \| 'other' \| null | no | Normalized statistical test category. |
| `test_method_raw` | str \| null | no | The source's own label for the test, e.g. "M1.1", "pLoF\|0.001". |
| `variant_mask_raw` | str \| null | no | The source's own rare-variant mask/collapsing label, if any. |
| `variant_mask_class` | 'plof' \| 'plof_missense' \| 'missense' \| 'synonymous_control' \| 'ultra_rare' \| 'other' \| null | no | Normalized variant mask class, decoded via S4's mask_definitions. |
| `maf_threshold` | float \| null | no | Minor allele frequency threshold applied by the mask, if any. |
| `effect_value` | float \| null | no | The reported effect size. |
| `effect_type` | 'beta' \| 'odds_ratio' \| 'log_odds' \| 'hazard_ratio' \| 'z_score' \| null | no | What kind of statistic effect_value is. |
| `effect_allele` | str \| null | no | The allele effect_value is reported with respect to. CRITICAL: never inferred -- null whenever the source doesn't state it unambiguously. Getting this wrong silently flips effect_direction. |
| `other_allele` | str \| null | no | The non-effect allele. |
| `effect_direction` | 'increases' \| 'decreases' \| 'unknown' \| null | no | Direction of effect on the trait. "unknown" whenever effect_allele couldn't be determined -- never inferred from effect_value's sign alone. |
| `standard_error` | float \| null | no | Standard error of effect_value. |
| `p_value` | float \| null | no | Reported p-value. |
| `ci_lower` | float \| null | no | Lower bound of the confidence interval. |
| `ci_upper` | float \| null | no | Upper bound of the confidence interval. |
| `cohort_name` | str \| null | no | Cohort/biobank name, e.g. "UK Biobank", "Geisinger DiscovEHR". |
| `ancestry` | str \| null | no | Ancestry group, e.g. EUR \| AFR \| SAS \| EAS \| AMR \| pan \| other. |
| `n_total` | int \| null | no | Total sample size for this test. |
| `n_cases` | int \| null | no | Case count, for binary traits. |
| `n_controls` | int \| null | no | Control count, for binary traits. |
| `n_carriers` | int \| null | no | Variant-carrier count, for burden/collapsing tests. |
| `analysis_role` | 'discovery' \| 'replication' \| 'meta' \| 'unknown' \| null | no | Whether this result is a discovery, replication, or meta-analysis finding. |
| `confidence` | float | yes | 0-1 confidence score, computed by S9 arbitration. |
| `check_results` | list[CheckResult] | no | S8 validation checks that ran against this record. May be empty. |
| `review_status` | 'auto_pass' \| 'needs_review' \| 'human_confirmed' \| 'human_corrected' \| 'rejected' | yes | Routing decision from S9 arbitration. Only auto_pass and human_confirmed records ship in S10's main dataset; needs_review is quarantined separately; rejected records are dropped from publication entirely. |
| `strand_ambiguous` | bool | no | True for A/T or C/G variants, where strand can't be determined from the document alone and effect_allele may be flipped even when it appears resolvable. Set by S7 normalization. |
