"""Outputs of S8 (validation)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

CheckStatus = Literal["pass", "fail", "warn", "skip"]
CheckName = Literal[
    "v1_arithmetic",
    "v2_grounding",
    "v3_semantic_effect_allele",
    "v3_semantic_effect_direction",
    "v3_semantic_trait_type",
    "v3_semantic_analysis_role",
]


class CheckResult(BaseModel):
    check_name: CheckName
    status: CheckStatus
    detail: str
    checked_by: str  # "code" or "llm:<model>"


class V3SemanticVerdict(BaseModel):
    """One model's answers to S8's four semantic (V3) questions, asked as
    separate structured fields in a single call rather than four separate
    API calls -- same information, a quarter of the token cost. S8 fans
    this out into four CheckResult rows (one per question) so S9 can weigh
    them individually, and runs it with two model families whose verdicts
    are reported separately rather than averaged.
    """

    effect_allele_correct: bool
    effect_allele_reasoning: str
    effect_direction_consistent: bool
    effect_direction_reasoning: str
    trait_type_and_effect_type_appropriate: bool
    trait_type_reasoning: str
    analysis_role_correct: bool
    analysis_role_reasoning: str


class ValidationVerdict(BaseModel):
    """Aggregate verdict for one record, feeding S9 arbitration."""

    record_id: str
    check_results: list[CheckResult]

    @property
    def any_fail(self) -> bool:
        return any(c.status == "fail" for c in self.check_results)

    @property
    def any_warn(self) -> bool:
        return any(c.status == "warn" for c in self.check_results)
