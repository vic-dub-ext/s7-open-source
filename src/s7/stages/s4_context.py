"""S4 - Context assembly (deterministic, no LLM). In: the parsed article.
Out: a MethodsBundle per paper -- the text S5 needs to interpret columns.


The article artifact is Europe PMC's JATS XML, which Extend's document
parser doesn't meaningfully convert (it isn't a visual/layout document, so
S2's parse just echoes the markup back). JATS is a well-defined, explicitly
sectioned format, so this stage parses the raw XML directly with the
standard library instead of relying on S2's output for the article -- more
precise, and it doesn't cost an Extend call.

Priority order when the ~30k token budget is tight:
table captions > mask definitions > statistical analysis subsection > rest
of methods.
"""

from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from sqlalchemy import Connection

from s7.models.stage import StageResult, StageStatus
from s7.store import events
from s7.store.artifacts import list_top_level
from s7.store.classifications import list_by_effective_classification
from s7.store.context import (
    delete_methods_bundles_for_run,
    has_methods_bundle,
    insert_methods_bundle,
)
from s7.store.db import get_engine
from s7.store.parsed import list_parsed_cells, list_parsed_tables_for_artifact
from s7.store.runs import get_run, update_run_status

STAGE = "s4_context"

TOKEN_BUDGET = 30_000
CHARS_PER_TOKEN = 4  # rough heuristic; exact tokenization isn't worth a dependency here
CHAR_BUDGET = TOKEN_BUDGET * CHARS_PER_TOKEN

STAT_SUBSECTION_KEYWORDS = re.compile(
    r"associat|statistic|regression|burden test|genetic analys", re.IGNORECASE
)
DECODER_CLASSIFICATIONS = ["mask_definitions", "phenotype_definitions"]


def estimate_tokens(text: str) -> int:
    return len(text) // CHARS_PER_TOKEN


def _clean_text(el: ET.Element) -> str:
    return " ".join(t.strip() for t in el.itertext() if t.strip())


def _title_of(sec: ET.Element) -> str:
    title_el = sec.find("./title")
    return (title_el.text or "").strip() if title_el is not None and title_el.text else ""


def _extract_jats_sections(xml_bytes: bytes) -> dict[str, str] | None:
    """Returns {"methods": ..., "stat_subsection": ..., "table_captions": ...},
    or None if this isn't parseable JATS XML.
    """
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None

    body = root.find(".//body")
    if body is None:
        return None

    methods_sec = None
    supplementary_sec = None
    for sec in body.findall("./sec"):
        title = _title_of(sec).lower()
        if title == "methods" and methods_sec is None:
            methods_sec = sec
        elif title == "supplementary information" and supplementary_sec is None:
            supplementary_sec = sec

    if methods_sec is None:
        return None

    stat_subsection = ""
    for sub in methods_sec.findall("./sec"):
        if STAT_SUBSECTION_KEYWORDS.search(_title_of(sub)):
            stat_subsection = _clean_text(sub)
            break

    methods_text = _clean_text(methods_sec)
    table_captions = _clean_text(supplementary_sec) if supplementary_sec is not None else ""

    return {
        "methods": methods_text,
        "stat_subsection": stat_subsection,
        "table_captions": table_captions,
    }


def _render_parsed_table(header_rows: list[Any], cells: list[dict[str, Any]]) -> str:
    by_row: dict[int, dict[int, str]] = {}
    for c in cells:
        by_row.setdefault(c["row_index"], {})[c["col_index"]] = c["value"] or ""

    header_width = max((len(row) for row in header_rows), default=0)
    cell_width = max((col for row in by_row.values() for col in row), default=-1) + 1
    width = max(header_width, cell_width)

    lines = []
    for row in header_rows:
        padded = list(row) + [None] * (width - len(row))
        lines.append(" | ".join(str(c) if c is not None else "" for c in padded))
    if header_rows:
        lines.append("---")
    for row_index in sorted(by_row):
        row = by_row[row_index]
        lines.append(" | ".join(row.get(i, "") for i in range(width)))
    return "\n".join(lines)


MAX_DECODER_ROWS = 60  # per artifact -- a decoder ring needs its structure, not every row


def _render_decoder_artifact(conn: Connection, artifact: dict[str, Any]) -> tuple[str, int]:
    """Returns (rendered_text, total_row_count). Rendering is capped at
    MAX_DECODER_ROWS: a definitions table's *shape* is what interprets mask
    notation like M1.1, not necessarily all 3,994 rows of it (e.g. a
    phenotype_definitions table can be the paper's full trait list).
    """
    tables = list_parsed_tables_for_artifact(conn, artifact["artifact_id"])
    header = f"### {artifact['file_name']}"
    if artifact.get("sheet_name"):
        header += f" :: {artifact['sheet_name']}"
    parts = [header]
    total_rows = 0
    rows_used = 0
    for t in tables:
        header_rows = json.loads(t["header_rows_json"])
        cells = list_parsed_cells(conn, t["id"])
        total_rows += t["row_count"]
        if not header_rows and not cells:
            continue
        if rows_used >= MAX_DECODER_ROWS:
            continue
        budget = MAX_DECODER_ROWS - rows_used
        kept = set(sorted({c["row_index"] for c in cells})[:budget])
        rows_used += len(kept)
        capped_cells = [c for c in cells if c["row_index"] in kept]
        parts.append(_render_parsed_table(header_rows, capped_cells))
    if total_rows > rows_used:
        parts.append(f"... ({total_rows - rows_used} more rows omitted)")
    return "\n".join(parts), total_rows


def _fit_budget(segments: list[tuple[str, str]]) -> str:
    """segments are (label, text) in priority order. Includes as many whole
    segments as fit; truncates the first one that doesn't.
    """
    out: list[str] = []
    remaining = CHAR_BUDGET
    for label, text in segments:
        if not text:
            continue
        header = f"## {label}\n\n"
        budget_for_text = remaining - len(header)
        if len(text) <= budget_for_text:
            out.append(header + text)
            remaining -= len(header) + len(text)
        elif budget_for_text > 200:  # only bother truncating if meaningful room is left
            out.append(f"## {label} (truncated)\n\n{text[:budget_for_text]}")
            remaining = 0
            break
        else:
            break
    return "\n\n".join(out)


async def run(run_id: str, *, force: bool = False) -> StageResult:
    engine = get_engine()

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        already = has_methods_bundle(conn, run_id)
        if not force and already:
            return StageResult(stage=STAGE, status="skipped", counts={})
        if force and already:
            delete_methods_bundles_for_run(conn, run_id)

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S4 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)

        top_level = list_top_level(conn, run_id)
        article = next((a for a in top_level if a["kind"] == "article"), None)
        decoders = list_by_effective_classification(conn, run_id, DECODER_CLASSIFICATIONS)

        source_artifact_ids: list[str] = []
        methods_text = ""
        stat_subsection = ""
        table_captions = ""

        if article is not None:
            source_artifact_ids.append(article["id"])
            try:
                xml_bytes = Path(str(article["storage_path"])).read_bytes()
            except OSError:
                xml_bytes = b""
            sections = _extract_jats_sections(xml_bytes) if xml_bytes else None
            if sections is not None:
                methods_text = sections["methods"]
                stat_subsection = sections["stat_subsection"]
                table_captions = sections["table_captions"]
            else:
                events.emit(
                    conn,
                    run_id=run_id,
                    stage=STAGE,
                    event_type="error",
                    level="warn",
                    message=f"{article['file_name']} is not parseable JATS XML; "
                    "methods bundle will have no article text",
                )

        # Rest-of-methods excludes the stat subsection so it isn't duplicated.
        rest_of_methods = methods_text
        if stat_subsection:
            rest_of_methods = methods_text.replace(stat_subsection, "")

        # Render smallest decoder tables first: a compact lookup like the mask
        # nomenclature table matters more per-byte than a table that happens to
        # also be classified phenotype_definitions but is really a 4,000-row
        # trait list -- and the latter must not be allowed to crowd out both
        # the former and the Methods text below it in priority.
        rendered_decoders = [
            (d["artifact_id"], _render_decoder_artifact(conn, d)) for d in decoders
        ]
        rendered_decoders.sort(key=lambda item: item[1][1])
        for artifact_id, _ in rendered_decoders:
            source_artifact_ids.append(artifact_id)
        mask_definitions_text = "\n\n".join(
            text for _, (text, _) in rendered_decoders if text
        )

        content = _fit_budget(
            [
                ("Table captions", table_captions),
                ("Mask and phenotype definitions", mask_definitions_text),
                ("Statistical analysis", stat_subsection),
                ("Methods", rest_of_methods),
            ]
        )
        token_count = estimate_tokens(content)

        insert_methods_bundle(
            conn,
            run_id=run_id,
            content=content,
            token_count=token_count,
            source_artifact_ids=source_artifact_ids,
        )

        status: StageStatus = "done" if content else "partial"
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S4 context finished: ~{token_count} tokens from "
            f"{len(source_artifact_ids)} source artifacts",
            payload={"token_count": token_count, "source_artifacts": len(source_artifact_ids)},
        )

    return StageResult(
        stage=STAGE,
        status=status,
        counts={"token_count": token_count, "source_artifacts": len(source_artifact_ids)},
    )
