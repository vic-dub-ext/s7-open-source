"""Artifacts (S0-S1) and parsed tables (S2)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from s7.models.provenance import CellCoordinate

ArtifactKind = Literal["article", "supplement", "sheet"]
SkipReason = Literal[
    "access_denied", "too_large", "empty_sheet", "unsupported_type", "download_failed"
]

# Stable classification taxonomy IDs.
ClassificationId = Literal[
    "assoc_gene_level",
    "assoc_variant_level",
    "assoc_conditional",
    "assoc_replication",
    "mask_definitions",
    "phenotype_definitions",
    "cohort_description",
    "qc_metrics",
    "other",
]


class RowColOffset(BaseModel):
    """Maps a row/col in an exploded single-sheet file back to the original workbook."""

    row_offset: int = 0
    col_offset: int = 0


class Artifact(BaseModel):
    id: str
    run_id: str
    kind: ArtifactKind
    file_name: str
    mime_type: str
    byte_size: int
    sha256: str
    download_url: str | None = None
    retrieved_at: datetime
    storage_path: str

    # set for sheets produced by S1's explode step
    parent_artifact_id: str | None = None
    sheet_name: str | None = None
    offset: RowColOffset | None = None
    is_classify_sample: bool = False

    # true dimensions of the sheet, even when is_classify_sample truncated the
    # stored file -- S2 parses the full sheet regardless of what S1 uploaded
    row_count: int | None = None
    col_count: int | None = None

    skip_reason: SkipReason | None = None
    skip_detail: str | None = None


class ParsedCell(BaseModel):
    row_index: int
    col_index: int
    value: str | None
    coordinate: CellCoordinate


class ParsedTable(BaseModel):
    id: str
    run_id: str
    artifact_id: str
    extend_parse_run_id: str
    target: Literal["markdown", "spatial"]
    content: str | None = None  # full text for non-table chunks, e.g. article markdown
    header_rows: list[list[str | None]]  # preserved as a block; S5 interprets, S2 never flattens
    row_count: int
    col_count: int
    raw_response_path: str  # verbatim Extend response, stored alongside the normalized form
    created_at: datetime


class SheetClassification(BaseModel):
    id: str
    run_id: str
    artifact_id: str
    classification_id: ClassificationId
    confidence: float
    insights: str  # Extend's reasoning string, shown in the UI
    processor_id: str
    processor_version: str
    retried: bool = False
    needs_review: bool = False  # confidence < 0.75 after retry
    human_override_class: ClassificationId | None = None
    created_at: datetime
