"""Renders a slice of a source spreadsheet as a plain value grid, for the
parse inspector's side-by-side view.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from pathlib import Path

import openpyxl
from openpyxl.utils.exceptions import InvalidFileException

MAX_RENDER_ROWS = 250
MAX_RENDER_COLS = 40


def read_sheet_grid(
    storage_path: str, *, start_row: int = 0, row_limit: int = MAX_RENDER_ROWS
) -> list[list[object]] | None:
    """Rows [start_row, start_row + row_limit) of the sheet, values only.
    Windowed rather than always-from-zero: a table fragment detected deep
    into a large sheet (row 2000+) would otherwise never have its source
    rows on screen. Returns None if the file can't be opened as a workbook.

    Reads via BytesIO rather than handing openpyxl the path directly --
    content-addressed storage paths have no file extension, and openpyxl's
    path-based loader sniffs format from the extension and rejects anything
    it doesn't recognize even when the bytes are a perfectly valid xlsx.
    """
    try:
        data = Path(storage_path).read_bytes()
        wb = openpyxl.load_workbook(BytesIO(data), read_only=True, data_only=True)
    except (InvalidFileException, KeyError, OSError, zipfile.BadZipFile):
        return None

    try:
        ws = wb.worksheets[0]
        rows: list[list[object]] = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            if i < start_row:
                continue
            if i >= start_row + row_limit:
                break
            rows.append(list(row[:MAX_RENDER_COLS]))
        return rows
    finally:
        wb.close()


def row_window_for_cells(rows: list[int], *, padding: int = 5) -> int:
    """Start row for a window that comfortably contains every given row index."""
    if not rows:
        return 0
    return max(0, min(rows) - padding)


__all__ = ["MAX_RENDER_ROWS", "read_sheet_grid", "row_window_for_cells"]
