"""S2 - Parse (Extend). In: artifacts (S1's leaf output). Out: ParsedTable
rows with cell-level content and coordinates.

One Extend parse run per artifact. A parse run's output can contain more than
one table block (e.g. a title/caption row detected as its own table, or a
sheet with several distinct regions) -- each table block becomes its own
ParsedTable row, all sharing one extend_parse_run_id, so row/col coordinates
never collide across tables. Non-table content (article text, a supplement
PDF with no tables) becomes one ParsedTable row with `content` set and no
cells.

Multi-row headers are preserved as a block (`header_rows`), not flattened --
S5 interprets them later.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import Connection, Engine

from s7.config import Settings, get_settings
from s7.models.stage import StageResult, StageStatus
from s7.providers.extend import ExtendClient, ExtendError, config_hash
from s7.storage import store_bytes
from s7.store import events
from s7.store.artifacts import list_parse_targets
from s7.store.db import get_engine, now_iso
from s7.store.extend_cache import (
    get_cached_file_id,
    get_cached_parse,
    store_parse_cache,
    store_uploaded_file,
)
from s7.store.parsed import (
    delete_parsed_for_run,
    has_parsed_tables,
    insert_parsed_cells,
    insert_parsed_table,
)
from s7.store.provider_calls import record_provider_call
from s7.store.runs import get_run, update_run_status

STAGE = "s2_parse"

# target: markdown for article PDFs (readable methods text); table-bearing
# artifacts additionally request cell-level blocks so provenance is real.
ARTICLE_CONFIG: dict[str, Any] = {"target": "markdown"}
TABLE_CONFIG: dict[str, Any] = {
    "target": "markdown",
    "block_options": {"tables": {"cell_blocks_enabled": True}},
    "advanced_options": {
        "excel_parsing_mode": "advanced",
        "excel_include_cell_metadata": True,
    },
}

MAX_CONCURRENCY = 5
PARSE_TIMEOUT_S = 300.0


def _table_from_block(block: dict[str, Any]) -> dict[str, Any]:
    """Split one Extend table block's children into header rows (isHeader)
    and data cells, each cell keeping Extend's own row/col index verbatim --
    that coordinate is the provenance ground truth, not something to renumber.
    """
    details = block.get("details") or {}
    header_map: dict[int, dict[int, str | None]] = {}
    cells: list[dict[str, Any]] = []
    for child in block.get("children") or []:
        cdetails = child.get("details") or {}
        row_index = cdetails.get("rowIndex")
        col_index = cdetails.get("columnIndex")
        if row_index is None or col_index is None:
            continue
        value = child.get("content")
        if cdetails.get("isHeader"):
            header_map.setdefault(row_index, {})[col_index] = value
            continue
        bbox = child.get("boundingBox") or {}
        page = (child.get("metadata") or {}).get("page") or {}
        cells.append(
            {
                "row_index": row_index,
                "col_index": col_index,
                "value": value,
                "page": page.get("number"),
                "bbox_x0": bbox.get("left"),
                "bbox_y0": bbox.get("top"),
                "bbox_x1": bbox.get("right"),
                "bbox_y1": bbox.get("bottom"),
            }
        )
    header_rows = []
    for row_index in sorted(header_map):
        row = header_map[row_index]
        width = max(row) + 1 if row else 0
        header_rows.append([row.get(c) for c in range(width)])
    return {
        "content": block.get("content"),
        "row_count": details.get("rowCount", 0),
        "col_count": details.get("columnCount", 0),
        "header_rows": header_rows,
        "cells": cells,
    }


def _tables_and_fallback(output: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """Every table block in the output, plus the concatenated text of
    everything else (used as `content` when there are no tables at all).
    """
    tables: list[dict[str, Any]] = []
    text_parts: list[str] = []
    for chunk in output.get("chunks") or []:
        for block in chunk.get("blocks") or []:
            if block.get("type") == "table":
                tables.append(_table_from_block(block))
            else:
                content = block.get("content")
                if content:
                    text_parts.append(content)
    return tables, "\n\n".join(text_parts)


def _persist_tables(
    conn: Connection,
    *,
    run_id: str,
    artifact: dict[str, Any],
    extend_parse_run_id: str,
    target: str,
    output: dict[str, Any],
    raw_response_path: str,
) -> int:
    tables, fallback_text = _tables_and_fallback(output)
    is_sheet = artifact["kind"] == "sheet"
    row_offset = int(artifact.get("row_offset") or 0)
    col_offset = int(artifact.get("col_offset") or 0)

    if not tables:
        insert_parsed_table(
            conn,
            run_id=run_id,
            artifact_id=artifact["id"],
            extend_parse_run_id=extend_parse_run_id,
            target=target,
            content=fallback_text or None,
            header_rows=[],
            row_count=0,
            col_count=0,
            raw_response_path=raw_response_path,
            created_at=now_iso(),
        )
        return 1

    for table in tables:
        table_id = insert_parsed_table(
            conn,
            run_id=run_id,
            artifact_id=artifact["id"],
            extend_parse_run_id=extend_parse_run_id,
            target=target,
            content=table["content"],
            header_rows=table["header_rows"],
            row_count=table["row_count"],
            col_count=table["col_count"],
            raw_response_path=raw_response_path,
            created_at=now_iso(),
        )
        cells = []
        for cell in table["cells"]:
            cell = dict(cell)
            if is_sheet:
                cell["sheet_row"] = cell["row_index"] + row_offset
                cell["sheet_col"] = cell["col_index"] + col_offset
            cells.append(cell)
        insert_parsed_cells(conn, table_id, cells)
    return len(tables)


async def _get_or_create_parse(
    engine: Engine,
    client: ExtendClient,
    settings: Settings,
    *,
    run_id: str,
    stage_entity_id: str,
    artifact: dict[str, Any],
    config: dict[str, Any],
    cfg_hash: str,
) -> tuple[dict[str, Any] | None, str, str, float, bool]:
    """Returns (raw_output_doc, extend_parse_run_id, raw_response_path, credits, cache_hit).
    `raw_output_doc` is None when the run failed (already logged by the caller).
    """
    sha256 = artifact["sha256"]

    with engine.begin() as conn:
        cached = get_cached_parse(conn, sha256, cfg_hash)
    if cached is not None and cached["status"] == "PROCESSED":
        raw_response_path = str(cached["raw_response_path"])
        raw = json.loads(Path(raw_response_path).read_text())
        return raw, str(cached["parse_run_id"]), raw_response_path, 0.0, True

    with engine.begin() as conn:
        file_id = get_cached_file_id(conn, sha256)
    if file_id is None:
        data = Path(str(artifact["storage_path"])).read_bytes()
        file = await client.upload_file(
            file_name=str(artifact["file_name"]), data=data, mime_type=str(artifact["mime_type"])
        )
        file_id = file.id
        with engine.begin() as conn:
            store_uploaded_file(conn, sha256=sha256, file_id=file_id, credits=0.0)

    parse_run_id = await client.create_parse_run(file_id=file_id, config=config)
    # Persisted before polling starts, so a crashed process resumes by polling
    # this ID rather than re-dispatching the run.
    with engine.begin() as conn:
        store_parse_cache(
            conn,
            sha256=sha256,
            config_hash=cfg_hash,
            parse_run_id=parse_run_id,
            status="PROCESSING",
            raw_response_path=None,
            credits=0.0,
        )

    run = await client.wait_for_parse_run(parse_run_id, timeout=PARSE_TIMEOUT_S)
    credits = float(run.usage.credits) if run.usage and run.usage.credits else 0.0

    if run.status != "PROCESSED":
        with engine.begin() as conn:
            store_parse_cache(
                conn,
                sha256=sha256,
                config_hash=cfg_hash,
                parse_run_id=parse_run_id,
                status=run.status,
                raw_response_path=None,
                credits=credits,
            )
            events.emit(
                conn,
                run_id=run_id,
                stage=STAGE,
                entity_id=stage_entity_id,
                event_type="error",
                level="error",
                message=f"parse failed for {artifact['file_name']}: "
                f"{run.failure_reason or run.status}",
            )
        return None, run.id, "", credits, False

    raw = run.model_dump(mode="json", by_alias=True)
    raw_bytes = json.dumps(raw).encode()
    _, raw_path = store_bytes(settings.parsed_raw_dir, raw_bytes)
    with engine.begin() as conn:
        store_parse_cache(
            conn,
            sha256=sha256,
            config_hash=cfg_hash,
            parse_run_id=parse_run_id,
            status="PROCESSED",
            raw_response_path=str(raw_path),
            credits=credits,
        )
    return raw, run.id, str(raw_path), credits, False


async def _parse_one(
    engine: Engine,
    client: ExtendClient,
    settings: Settings,
    *,
    run_id: str,
    artifact: dict[str, Any],
    counts: dict[str, int],
    counts_lock: asyncio.Lock,
) -> None:
    artifact_id = artifact["id"]
    config = ARTICLE_CONFIG if artifact["kind"] == "article" else TABLE_CONFIG
    cfg_hash = config_hash(config)
    started = time.monotonic()

    try:
        raw, extend_parse_run_id, raw_response_path, credits, cache_hit = (
            await _get_or_create_parse(
                engine,
                client,
                settings,
                run_id=run_id,
                stage_entity_id=artifact_id,
                artifact=artifact,
                config=config,
                cfg_hash=cfg_hash,
            )
        )
        if raw is None:
            async with counts_lock:
                counts["failed"] += 1
            return

        with engine.begin() as conn:
            table_count = _persist_tables(
                conn,
                run_id=run_id,
                artifact=artifact,
                extend_parse_run_id=extend_parse_run_id,
                target=str(config["target"]),
                output=raw.get("output") or {},
                raw_response_path=raw_response_path,
            )
            record_provider_call(
                conn,
                run_id=run_id,
                stage=STAGE,
                provider="extend",
                operation="parse",
                cost_credits=credits,
                cached=cache_hit,
                latency_ms=int((time.monotonic() - started) * 1000),
                ok=True,
            )
            sheet_suffix = f" ({artifact['sheet_name']})" if artifact.get("sheet_name") else ""
            events.emit(
                conn,
                run_id=run_id,
                stage=STAGE,
                entity_id=artifact_id,
                event_type="parse_completed",
                message=f"parsed {artifact['file_name']}{sheet_suffix}",
                payload={"tables": table_count, "credits": credits, "cached": cache_hit},
            )
        async with counts_lock:
            counts["parsed"] += 1
            if cache_hit:
                counts["cached"] += 1

    except (ExtendError, httpx.HTTPError, OSError) as exc:
        with engine.begin() as conn:
            events.emit(
                conn,
                run_id=run_id,
                stage=STAGE,
                entity_id=artifact_id,
                event_type="error",
                level="error",
                message=f"parse errored for {artifact['file_name']}: {exc}",
            )
        async with counts_lock:
            counts["failed"] += 1


async def run(run_id: str, *, force: bool = False) -> StageResult:
    settings = get_settings()
    settings.ensure_dirs()
    settings.require("extend_api_key", stage=STAGE)
    engine = get_engine()

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        already = has_parsed_tables(conn, run_id)
        if not force and already:
            return StageResult(stage=STAGE, status="skipped", counts={})
        if force and already:
            delete_parsed_for_run(conn, run_id)

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S2 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)
        targets = list_parse_targets(conn, run_id)

    counts = {"parsed": 0, "failed": 0, "cached": 0}
    counts_lock = asyncio.Lock()
    semaphore = asyncio.Semaphore(MAX_CONCURRENCY)

    async def bounded(artifact: dict[str, Any], client: ExtendClient) -> None:
        async with semaphore:
            await _parse_one(
                engine,
                client,
                settings,
                run_id=run_id,
                artifact=artifact,
                counts=counts,
                counts_lock=counts_lock,
            )

    if targets:
        async with httpx.AsyncClient(timeout=60.0) as http_client:
            client = ExtendClient(settings, stage=STAGE, httpx_client=http_client)
            await asyncio.gather(*(bounded(a, client) for a in targets))

    status: StageStatus = "done" if counts["failed"] == 0 else "partial"
    with engine.begin() as conn:
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S2 parse finished: {counts['parsed']} parsed "
            f"({counts['cached']} cached), {counts['failed']} failed",
            payload=counts,
        )

    return StageResult(stage=STAGE, status=status, counts=counts)
