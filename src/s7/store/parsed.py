"""CRUD for S2's output: `parsed_tables` and `parsed_cells`."""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Connection, text

from s7.store.db import new_id


def insert_parsed_table(
    conn: Connection,
    *,
    run_id: str,
    artifact_id: str,
    extend_parse_run_id: str,
    target: str,
    content: str | None,
    header_rows: list[list[str | None]],
    row_count: int,
    col_count: int,
    raw_response_path: str,
    created_at: str,
) -> str:
    table_id = new_id()
    conn.execute(
        text(
            "INSERT INTO parsed_tables (id, run_id, artifact_id, extend_parse_run_id, target, "
            "content, header_rows_json, row_count, col_count, raw_response_path, created_at) "
            "VALUES (:id, :run_id, :artifact_id, :extend_parse_run_id, :target, :content, "
            ":header_rows_json, :row_count, :col_count, :raw_response_path, :created_at)"
        ),
        {
            "id": table_id,
            "run_id": run_id,
            "artifact_id": artifact_id,
            "extend_parse_run_id": extend_parse_run_id,
            "target": target,
            "content": content,
            "header_rows_json": json.dumps(header_rows),
            "row_count": row_count,
            "col_count": col_count,
            "raw_response_path": raw_response_path,
            "created_at": created_at,
        },
    )
    return table_id


def insert_parsed_cells(
    conn: Connection, parsed_table_id: str, cells: list[dict[str, Any]]
) -> None:
    if not cells:
        return
    rows = [
        {
            "id": new_id(),
            "parsed_table_id": parsed_table_id,
            "row_index": c["row_index"],
            "col_index": c["col_index"],
            "value": c["value"],
            "sheet_row": c.get("sheet_row"),
            "sheet_col": c.get("sheet_col"),
            "page": c.get("page"),
            "bbox_x0": c.get("bbox_x0"),
            "bbox_y0": c.get("bbox_y0"),
            "bbox_x1": c.get("bbox_x1"),
            "bbox_y1": c.get("bbox_y1"),
        }
        for c in cells
    ]
    conn.execute(
        text(
            "INSERT INTO parsed_cells (id, parsed_table_id, row_index, col_index, value, "
            "sheet_row, sheet_col, page, bbox_x0, bbox_y0, bbox_x1, bbox_y1) VALUES "
            "(:id, :parsed_table_id, :row_index, :col_index, :value, :sheet_row, :sheet_col, "
            ":page, :bbox_x0, :bbox_y0, :bbox_x1, :bbox_y1)"
        ),
        rows,
    )


def delete_parsed_for_run(conn: Connection, run_id: str) -> None:
    conn.execute(
        text(
            "DELETE FROM parsed_cells WHERE parsed_table_id IN "
            "(SELECT id FROM parsed_tables WHERE run_id = :run_id)"
        ),
        {"run_id": run_id},
    )
    conn.execute(text("DELETE FROM parsed_tables WHERE run_id = :run_id"), {"run_id": run_id})


def has_parsed_tables(conn: Connection, run_id: str) -> bool:
    count = conn.execute(
        text("SELECT COUNT(*) FROM parsed_tables WHERE run_id = :run_id"), {"run_id": run_id}
    ).scalar_one()
    return bool(count)


def list_parsed_tables_for_run(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text("SELECT * FROM parsed_tables WHERE run_id = :run_id ORDER BY created_at"),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]


def list_parsed_artifact_summary(conn: Connection, run_id: str) -> list[dict[str, Any]]:
    """One row per parsed artifact, with how many table fragments it produced --
    the parse inspector's entry list.
    """
    rows = conn.execute(
        text(
            "SELECT a.id AS artifact_id, a.kind, a.file_name, a.sheet_name, "
            "COUNT(pt.id) AS table_count, SUM(pt.row_count) AS total_rows "
            "FROM artifacts a JOIN parsed_tables pt ON pt.artifact_id = a.id "
            "WHERE pt.run_id = :run_id "
            "GROUP BY a.id ORDER BY a.kind, a.file_name, a.sheet_name"
        ),
        {"run_id": run_id},
    )
    return [dict(r._mapping) for r in rows]


def list_parsed_tables_for_artifact(conn: Connection, artifact_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT * FROM parsed_tables WHERE artifact_id = :artifact_id ORDER BY created_at"
        ),
        {"artifact_id": artifact_id},
    )
    return [dict(r._mapping) for r in rows]


def get_parsed_table(conn: Connection, parsed_table_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        text("SELECT * FROM parsed_tables WHERE id = :id"), {"id": parsed_table_id}
    ).first()
    return dict(row._mapping) if row else None


def list_parsed_cells(conn: Connection, parsed_table_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        text(
            "SELECT * FROM parsed_cells WHERE parsed_table_id = :id "
            "ORDER BY row_index, col_index"
        ),
        {"id": parsed_table_id},
    )
    return [dict(r._mapping) for r in rows]


def get_cells_for_row(
    conn: Connection, parsed_table_id: str, row_index: int
) -> dict[int, str | None]:
    """col_index -> value for one row -- S8's V2 grounding check re-reads a
    sampled record's exact source cells without pulling the whole table.
    """
    rows = conn.execute(
        text(
            "SELECT col_index, value FROM parsed_cells "
            "WHERE parsed_table_id = :id AND row_index = :row_index"
        ),
        {"id": parsed_table_id, "row_index": row_index},
    )
    return {r[0]: r[1] for r in rows}
