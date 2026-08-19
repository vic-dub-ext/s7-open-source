"""S3's classification taxonomy. IDs are stable and are what we store in the
DB; `description` is prompt material for the Extend classifier and may be
retuned without a schema change.

No saved Extend classifier is used -- see `classify_config()` below and
providers/extend.py's `create_classify_run` docstring for why. The taxonomy
defined in this one file is the entire source of truth; there is nothing
else to provision.
"""

from __future__ import annotations

from typing import Any, TypedDict


class Classification(TypedDict):
    id: str
    type: str
    description: str


CLASSIFICATIONS: list[Classification] = [
    {
        "id": "assoc_gene_level",
        "type": "assoc_gene_level",
        "description": (
            "Association test results where the unit tested is a gene or a set of "
            "variants aggregated within a gene (burden, collapsing, SKAT, SKAT-O). "
            "Rows are genes."
        ),
    },
    {
        "id": "assoc_variant_level",
        "type": "assoc_variant_level",
        "description": (
            "Association test results where the unit tested is a single genetic "
            "variant. Rows are variants, usually with rsIDs or chr:pos:ref:alt "
            "identifiers."
        ),
    },
    {
        "id": "assoc_conditional",
        "type": "assoc_conditional",
        "description": (
            "Association results after conditioning on another signal, or "
            "leave-one-out analyses that re-test after removing a variant."
        ),
    },
    {
        "id": "assoc_replication",
        "type": "assoc_replication",
        "description": (
            "Association results from an independent cohort used to replicate a "
            "primary finding."
        ),
    },
    {
        "id": "mask_definitions",
        "type": "mask_definitions",
        "description": (
            "Definitions of variant masks, qualifying-variant criteria, or "
            "annotation filters used in the analysis. Not results."
        ),
    },
    {
        "id": "phenotype_definitions",
        "type": "phenotype_definitions",
        "description": (
            "Definitions of traits or phenotypes, e.g. ICD code mappings, "
            "case/control criteria. Not results."
        ),
    },
    {
        "id": "cohort_description",
        "type": "cohort_description",
        "description": (
            "Demographic or descriptive statistics about study participants. Not "
            "association results."
        ),
    },
    {
        "id": "qc_metrics",
        "type": "qc_metrics",
        "description": (
            "Quality control statistics: call rates, coverage, relatedness, "
            "principal components, genomic inflation."
        ),
    },
    {
        "id": "other",
        "type": "other",
        "description": "Anything else.",
    },
]

CLASSIFICATION_IDS = [c["id"] for c in CLASSIFICATIONS]

CLASSIFICATION_RULES = (
    "The most important distinction is whether each row reports a statistical test "
    "result (an effect size and a p-value) or reports a definition, a count, or a "
    "quality metric. When rows report test results, the next question is what was "
    "tested: if the row identifier is a gene symbol and the table mentions masks, "
    "burden, collapsing, or qualifying variants, classify as gene-level. If the row "
    "identifier is a variant identifier such as an rsID or a chromosome-position-"
    "allele string, classify as variant-level. Tables reporting results in a cohort "
    "explicitly described as independent, replication, or validation should be "
    "classified as replication even if they are otherwise gene-level or "
    "variant-level."
)

CONFIDENCE_THRESHOLD = 0.75

# Extend's two classify base processors (see ClassifyBaseProcessor in the
# extend_ai SDK): "light" is the cheap/fast first pass, "performance" is the
# more capable model reserved for the confidence-triggered retry in
# s3_classify.py. Verified live against the real API before relying on this
# split -- classification_light billed 0.3 credits for a 1-page file, and
# the run's usage breakdown confirmed the requested base_processor was the
# one actually charged.
BASE_PROCESSOR_FIRST_PASS = "classification_light"
BASE_PROCESSOR_RETRY = "classification_performance"


def classify_config(*, base_processor: str) -> dict[str, Any]:
    """The inline config sent on every classify_runs.create call -- see
    providers/extend.py's create_classify_run docstring for why this is
    inline rather than a saved classifier reference.
    """
    return {
        "classifications": [dict(c) for c in CLASSIFICATIONS],
        "classification_rules": CLASSIFICATION_RULES,
        "base_processor": base_processor,
    }
