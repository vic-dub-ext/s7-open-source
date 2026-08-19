"""Provenance primitives shared across stages.

Every published number must be traceable to file -> sheet -> row -> column.
These models are that trace, factored out so S1-S6 all speak the same
coordinate language instead of inventing ad hoc dicts.
"""

from __future__ import annotations

from pydantic import BaseModel


class SourceRef(BaseModel):
    """Identifies the document a piece of data came from."""

    source_doi: str
    source_pmcid: str | None = None
    source_file_name: str
    source_file_sha256: str
    source_sheet_name: str | None = None  # None for PDF-sourced tables


class CellCoordinate(BaseModel):
    """Where a single value lives, in the *original* artifact.

    Spreadsheets use row/col (0-based). PDFs use page + bounding box.
    S1's offset map is what lets a coordinate in an exploded single-sheet
    file be translated back into these original-workbook coordinates.
    """

    sheet_row: int | None = None
    sheet_col: int | None = None
    page: int | None = None
    bbox: tuple[float, float, float, float] | None = None  # x0, y0, x1, y1


class ProvenanceTriple(BaseModel):
    """The full chain from a published field back to one source cell."""

    source: SourceRef
    coordinate: CellCoordinate
    extend_parse_run_id: str
    schema_contract_id: str | None = None
