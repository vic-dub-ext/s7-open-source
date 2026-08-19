"""S7's LLM-facing trait disambiguation shape: anything below the ontology
match threshold is routed to an LLM mapping call with the top 10 OLS
candidates as options and an explicit "none of these" escape.
"""

from __future__ import annotations

from pydantic import BaseModel


class TraitDisambiguation(BaseModel):
    matched_obo_id: str | None  # one of the candidate obo_ids given, or null for "none of these"
    reasoning: str
