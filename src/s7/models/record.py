"""The canonical output row. Everything in the pipeline exists to produce
this correctly.

Every field is nullable except where marked required, because real papers
omit things. `*_raw` fields are sacred: never overwrite them with normalized
values.

Every field carries a `Field(description=...)` -- not just a comment --
because S10's data_dictionary.md is generated from these at publish time and
therefore cannot drift from the schema. A comment can't be introspected;
only the description that ships on the field itself can.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from s7.models.validation import CheckResult

REVIEW_STATUSES = (
    "auto_pass",
    "needs_review",
    "human_confirmed",
    "human_corrected",
    "rejected",
)
ReviewStatus = Literal[
    "auto_pass", "needs_review", "human_confirmed", "human_corrected", "rejected"
]


def record_id_for(
    *,
    source_file_sha256: str,
    parsed_table_id: str,
    source_row_index: int,
    entity_key: str,
    trait_raw: str,
) -> str:
    """uuid5 of source ref + entity + trait, so re-running a stage is idempotent.

    Includes `parsed_table_id`, not just `source_row_index` -- S5 commonly
    coalesces many `parsed_tables` fragments into one contract (Extend's
    block/chunk parsing splits one logical table into dozens of pieces; see
    s5_contract.py's `_group_tables_by_header`), and `row_index` restarts at
    0 in every fragment. Two genuinely different rows in different fragments
    of the same group can share a row_index -- and, we found by hand against
    real data, Extend's own `sheet_row` isn't sheet-global either, it's also
    fragment-local. Without the fragment id in the hash, such rows silently
    collide into one record and one is dropped as a false "duplicate".
    """
    name = f"{source_file_sha256}:{parsed_table_id}:{source_row_index}:{entity_key}:{trait_raw}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, name))


class AssociationRecord(BaseModel):
    # --- identity ---
    record_id: str = Field(
        description="Stable uuid5 of source file + fragment + row + entity + trait "
        "(see record_id_for). Re-running the pipeline on the same input reproduces "
        "the same record_id."
    )
    pipeline_version: str = Field(description="s7 package version that produced this record.")
    extracted_at: datetime = Field(description="When this record was projected (S6), UTC.")

    # --- provenance (required, all of it) ---
    source_doi: str = Field(description="DOI of the paper this result came from.")
    source_pmcid: str | None = Field(default=None, description="PubMed Central ID, if known.")
    source_file_name: str = Field(description="Original supplementary file name.")
    source_file_sha256: str = Field(
        description="SHA-256 of the source file, for content-addressed provenance."
    )
    source_sheet_name: str | None = Field(
        default=None,
        description="Workbook sheet name this row came from. None for PDF-sourced tables.",
    )
    source_row_index: int = Field(
        description="0-based row index within the parsed table fragment "
        "(source_parsed_table_id)."
    )
    source_page: int | None = Field(
        default=None, description="PDF page number, for PDF-sourced tables."
    )
    source_parsed_table_id: str | None = Field(
        default=None,
        description="The exact parsed_table fragment this row came from -- necessary because a "
        "row_index restarts at 0 in every fragment (see record_id_for's docstring).",
    )
    extend_parse_run_id: str = Field(description="Extend's own run id for the S2 parse call.")
    schema_contract_id: str = Field(
        description="The S5-induced schema contract used to project this row."
    )

    # --- entity: what was tested ---
    entity_type: Literal["gene", "variant"] = Field(
        description="Whether this row's test unit is a gene (burden/collapsing) or a "
        "single variant."
    )
    gene_symbol_raw: str | None = Field(
        default=None, description="Gene symbol exactly as printed in the source."
    )
    ensembl_gene_id: str | None = Field(
        default=None, description="Ensembl gene ID resolved from gene_symbol_raw (S7)."
    )
    variant_raw: str | None = Field(
        default=None, description="Variant identifier exactly as printed in the source."
    )
    chrom: str | None = Field(default=None, description="Chromosome, GRCh38.")
    pos_b38: int | None = Field(default=None, description="Position, lifted to GRCh38 if needed.")
    ref: str | None = Field(default=None, description="Reference allele.")
    alt: str | None = Field(default=None, description="Alternate allele.")
    rsid: str | None = Field(default=None, description="dbSNP rsID, resolved or as printed.")

    # --- trait: what it was tested against ---
    trait_raw: str = Field(description="Trait/phenotype label exactly as printed in the source.")
    trait_label: str | None = Field(
        default=None, description="Normalized trait label (S7 ontology resolution)."
    )
    efo_id: str | None = Field(
        default=None, description="Experimental Factor Ontology ID resolved from trait_raw (S7)."
    )
    trait_type: Literal["binary", "quantitative"] | None = Field(
        default=None,
        description="Whether the trait is a binary (case/control) or quantitative measure.",
    )
    trait_units: str | None = Field(
        default=None,
        description='Units for a quantitative trait, e.g. "mg/dL", "SD". None for binary.',
    )

    # --- test: how it was tested ---
    test_method: (
        Literal[
            "burden",
            "collapsing",
            "single_variant",
            "skat",
            "skat_o",
            "conditional",
            "leave_one_out",
            "meta_analysis",
            "other",
        ]
        | None
    ) = Field(default=None, description="Normalized statistical test category.")
    test_method_raw: str | None = Field(
        default=None,
        description='The source\'s own label for the test, e.g. "M1.1", "pLoF|0.001".',
    )
    variant_mask_raw: str | None = Field(
        default=None, description="The source's own rare-variant mask/collapsing label, if any."
    )
    variant_mask_class: (
        Literal["plof", "plof_missense", "missense", "synonymous_control", "ultra_rare", "other"]
        | None
    ) = Field(
        default=None,
        description="Normalized variant mask class, decoded via S4's mask_definitions.",
    )
    maf_threshold: float | None = Field(
        default=None, description="Minor allele frequency threshold applied by the mask, if any."
    )

    # --- statistics ---
    effect_value: float | None = Field(default=None, description="The reported effect size.")
    effect_type: (
        Literal["beta", "odds_ratio", "log_odds", "hazard_ratio", "z_score"] | None
    ) = Field(default=None, description="What kind of statistic effect_value is.")
    effect_allele: str | None = Field(
        default=None,
        description="The allele effect_value is reported with respect to. CRITICAL: never "
        "inferred -- null whenever the source doesn't state it unambiguously. "
        "Getting this wrong silently flips effect_direction.",
    )
    other_allele: str | None = Field(default=None, description="The non-effect allele.")
    effect_direction: Literal["increases", "decreases", "unknown"] | None = Field(
        default=None,
        description="Direction of effect on the trait. \"unknown\" whenever effect_allele "
        "couldn't be determined -- never inferred from effect_value's sign alone.",
    )
    standard_error: float | None = Field(
        default=None, description="Standard error of effect_value."
    )
    p_value: float | None = Field(default=None, description="Reported p-value.")
    ci_lower: float | None = Field(
        default=None, description="Lower bound of the confidence interval."
    )
    ci_upper: float | None = Field(
        default=None, description="Upper bound of the confidence interval."
    )

    # --- cohort ---
    cohort_name: str | None = Field(
        default=None, description='Cohort/biobank name, e.g. "UK Biobank", "Geisinger DiscovEHR".'
    )
    ancestry: str | None = Field(
        default=None, description="Ancestry group, e.g. EUR | AFR | SAS | EAS | AMR | pan | other."
    )
    n_total: int | None = Field(default=None, description="Total sample size for this test.")
    n_cases: int | None = Field(default=None, description="Case count, for binary traits.")
    n_controls: int | None = Field(default=None, description="Control count, for binary traits.")
    n_carriers: int | None = Field(
        default=None, description="Variant-carrier count, for burden/collapsing tests."
    )
    analysis_role: Literal["discovery", "replication", "meta", "unknown"] | None = Field(
        default=None,
        description="Whether this result is a discovery, replication, or meta-analysis finding.",
    )

    # --- quality ---
    confidence: float = Field(description="0-1 confidence score, computed by S9 arbitration.")
    check_results: list[CheckResult] = Field(
        default=[], description="S8 validation checks that ran against this record. May be empty."
    )
    review_status: ReviewStatus = Field(
        description="Routing decision from S9 arbitration. Only auto_pass and human_confirmed "
        "records ship in S10's main dataset; needs_review is quarantined separately; "
        "rejected records are dropped from publication entirely."
    )
    strand_ambiguous: bool = Field(
        default=False,
        description="True for A/T or C/G variants, where strand can't be determined from the "
        "document alone and effect_allele may be flipped even when it appears resolvable. "
        "Set by S7 normalization.",
    )
