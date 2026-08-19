"""Schema contract induction (S5) and its input bundle (S4).

An LLM reads a table's headers once and emits a SchemaContract. That contract
is then applied to every row by deterministic code (S6) -- row data is never
sent to an LLM for extraction.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel


class MethodsBundle(BaseModel):
    """The text S5 needs to interpret columns: methods, table captions, and
    mask/phenotype decoder tables. Capped at ~30k tokens; see s4_context.py
    for the truncation priority order.
    """

    id: str
    run_id: str
    content: str
    token_count: int
    source_artifact_ids: list[str]
    created_at: datetime


class ColumnMapping(BaseModel):
    source_column: str  # header text, exactly as parsed
    source_column_index: int
    target_field: str | None = None  # a field name on AssociationRecord, or None to ignore
    transform: (
        Literal[
            "identity",
            "neg_log10_to_p",
            "log_to_linear",
            "or_to_beta",
            "percent_to_fraction",
            "parse_ci_string",
        ]
        | None
    ) = None
    unit: str | None = None
    evidence: str  # quote or paraphrase from methods justifying this mapping
    confidence: float


class ConstantField(BaseModel):
    """One entry of ContractInduction.constant_fields. A plain dict[str, Any]
    would be the natural shape (and is what SchemaContract stores), but
    OpenAI's structured-output mode rejects an open-ended object schema
    (`additionalProperties: true`) outright, where Anthropic's SDK just
    strips the constraint -- so the LLM-facing shape has to be a list of
    key/value pairs instead. The stage converts this back to a dict before
    building the stored SchemaContract.
    """

    field: str  # a target field name, e.g. "cohort_name"
    value: str


class ContractInduction(BaseModel):
    """What one LLM call produces for S5. The fields that only make sense
    once the call has returned -- contract_id, parsed_table_id, model_spec,
    created_at -- are filled in by the stage afterward, not asked of the
    model; asking an LLM to invent an ID is a bug waiting to happen.
    """

    row_entity: Literal["gene", "variant", "gene_variant_pair"]
    constant_fields: list[ConstantField]
    column_mappings: list[ColumnMapping]
    effect_allele_source: Literal["column", "constant", "unresolvable"]
    effect_allele_column: str | None = None
    unmapped_columns: list[str]
    interpretation_notes: str
    overall_confidence: float


class SchemaContract(BaseModel):
    contract_id: str
    parsed_table_id: str
    model_spec: str  # "provider:model" that produced this contract
    row_entity: Literal["gene", "variant", "gene_variant_pair"]
    constant_fields: dict[str, Any]  # values true for every row, e.g. cohort_name, ancestry
    column_mappings: list[ColumnMapping]
    effect_allele_source: Literal["column", "constant", "unresolvable"]
    effect_allele_column: str | None = None
    unmapped_columns: list[str]
    interpretation_notes: str
    overall_confidence: float
    needs_review: bool = False
    created_at: datetime
