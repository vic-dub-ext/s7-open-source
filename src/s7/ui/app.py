"""FastAPI backend for `uv run s7 ui`. Jinja2 + HTMX + SSE, no build step.

The shell is the Runs list and a run detail page with the stage spine
(reading whatever counts already exist in the DB) plus the live log drawer.
Every per-stage inspector view (artifact browser, parse inspector, ...)
hangs off that same shell.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

from s7.config import ModelSpec
from s7.stages import STAGE_MODULES
from s7.store import events as event_store
from s7.store.artifacts import get_artifact, list_children, list_top_level
from s7.store.classifications import (
    bucket_counts_for_run,
    get_classification,
    list_classifications_for_run,
    set_human_override,
)
from s7.store.contracts import (
    list_column_mappings,
    list_contracts_for_agreement_group,
    list_contracts_for_run,
    list_member_table_ids,
)
from s7.store.db import get_engine
from s7.store.human_labels import insert_human_label
from s7.store.llm_calls import list_llm_calls_for_entity
from s7.store.parsed import (
    get_parsed_table,
    list_parsed_artifact_summary,
    list_parsed_cells,
    list_parsed_tables_for_artifact,
)
from s7.store.runs import STAGE_ORDER, create_run, get_run, list_runs, stage_counts
from s7.taxonomy import CLASSIFICATION_IDS
from s7.ui.grid import read_sheet_grid, row_window_for_cells

UI_DIR = Path(__file__).parent
app = FastAPI(title="s7")
app.mount("/static", StaticFiles(directory=UI_DIR / "static"), name="static")
templates = Jinja2Templates(directory=UI_DIR / "templates")
# Cache-busts app.css: a browser (or an intermediate proxy) that cached an
# earlier response for /static/app.css has no reason to know the file
# changed after a restart, since the URL never changes on its own. Tying
# the query string to the file's own mtime means it changes exactly when
# the file does, with no version number to remember to bump by hand.
templates.env.globals["css_version"] = int((UI_DIR / "static" / "app.css").stat().st_mtime)

STAGE_LABELS = {
    "s0_acquire": "S0 Acquire",
    "s1_explode": "S1 Explode",
    "s2_parse": "S2 Parse",
    "s3_classify": "S3 Classify",
    "s4_context": "S4 Context",
    "s5_contract": "S5 Contract",
    "s6_project": "S6 Project",
    "s7_normalize": "S7 Normalize",
    "s8_validate": "S8 Validate",
    "s9_arbitrate": "S9 Arbitrate",
    "s10_publish": "S10 Publish",
}
STAGE_CODES = {stage_id: f"S{i}" for i, stage_id in enumerate(STAGE_ORDER)}
# The log drawer renders client-side from raw SSE events, whose `stage`
# field is the full id ("s7_normalize") -- too wide for the log line's
# fixed-width stage column (see app.css's .log-line-stage). Embedding the
# same S0-S10 codes the rail uses keeps the two displays consistent.
templates.env.globals["stage_codes_json"] = json.dumps(STAGE_CODES)
STAGE_SHORT_LABELS = {
    "s0_acquire": "Acquire",
    "s1_explode": "Explode",
    "s2_parse": "Parse",
    "s3_classify": "Classify",
    "s4_context": "Context",
    "s5_contract": "Contract",
    "s6_project": "Project",
    "s7_normalize": "Normalize",
    "s8_validate": "Validate",
    "s9_arbitrate": "Arbitrate",
    "s10_publish": "Publish",
}
# The one number from each stage's counts dict (see store/runs.py's
# stage_counts) worth showing in the rail at a glance -- everything else in
# that dict is still visible in the stage's own panel.
STAGE_HEADLINE_KEY = {
    "s0_acquire": "found",
    "s1_explode": "sheets",
    "s2_parse": "tables",
    "s3_classify": "classified",
    "s4_context": "tokens",
    "s5_contract": "contracts",
    "s6_project": "records",
    "s7_normalize": "genes",
    "s8_validate": "v3_evaluated",
    "s9_arbitrate": "auto_pass",
    "s10_publish": "published",
}
_STATUS_RAN = {"done", "needs_review", "partial", "failed"}

# Short display labels + a distinct color per taxonomy bucket (see
# taxonomy.py's CLASSIFICATIONS -- same order, so index i here is always
# bucket i there). Colors are CSS custom properties (app.css's --bucket-N),
# not literal values, so the palette lives in exactly one place.
BUCKET_SHORT_LABELS = {
    "assoc_gene_level": "gene level",
    "assoc_variant_level": "variant level",
    "assoc_conditional": "conditional",
    "assoc_replication": "replication",
    "mask_definitions": "masks",
    "phenotype_definitions": "phenotypes",
    "cohort_description": "cohort",
    "qc_metrics": "qc metrics",
    "other": "other",
}
BUCKET_COLOR_VAR = {cid: f"var(--bucket-{i + 1})" for i, cid in enumerate(CLASSIFICATION_IDS)}
BUCKET_CAP_VAR = {cid: f"var(--bucket-{i + 1}-cap)" for i, cid in enumerate(CLASSIFICATION_IDS)}
# A global rather than per-route context so any template can look up a
# bucket's color (e.g. classification_row.html's swatch) without every
# route that might render it needing to remember to pass the map along.
templates.env.globals["bucket_color"] = lambda cid: BUCKET_COLOR_VAR.get(cid, "var(--bucket-9)")


def _build_buckets(conn: Any, run_id: str) -> dict[str, Any]:
    """Data for the S3 bucket chart -- every taxonomy category, in a fixed
    order, whether or not this run produced anything in it (an empty bucket
    is still worth seeing, dimmed, rather than silently absent). Bar
    heights are sqrt-scaled against this run's own max bucket so a small
    category stays visible next to a category 20x its size, while the
    printed count above each bar is always the exact number.
    """
    counts_by_bucket = {row["bucket"]: row for row in bucket_counts_for_run(conn, run_id)}
    raw_counts = [counts_by_bucket.get(cid, {}).get("count", 0) for cid in CLASSIFICATION_IDS]
    max_count = max(raw_counts) if any(raw_counts) else 1
    bar_h = 170

    buckets: list[dict[str, Any]] = []
    for cid in CLASSIFICATION_IDS:
        row = counts_by_bucket.get(cid, {})
        count = int(row.get("count", 0))
        review = int(row.get("needs_review_count", 0))
        frac = math.sqrt(count / max_count) if count else 0.0
        h = max(3, round(frac * bar_h)) if count else 0
        review_h = min(h, max(2, round(h * (review / count)))) if count and review else 0
        buckets.append(
            {
                "key": cid,
                "short": BUCKET_SHORT_LABELS[cid],
                "count": count,
                "count_str": f"{count:,}" if count else "·",
                "review": review,
                "color": BUCKET_COLOR_VAR[cid],
                "cap": BUCKET_CAP_VAR[cid],
                "h": h,
                "review_h": review_h,
                "is_empty": count == 0,
            }
        )
    total = sum(raw_counts)
    review_total = sum(b["review"] for b in buckets)
    return {
        "buckets": buckets,
        "total": total,
        "review_total": review_total,
        "axis_top": f"{max_count:,}",
    }


# Stages whose inspector is the shared artifact browser.
ARTIFACT_BROWSER_STAGES = {"s0_acquire", "s1_explode"}

# S0 Acquire is DOI-driven, not corpus-bound (see s0_acquire.py) -- any paper
# with a resolvable DOI works, not just the five fixed corpus entries. The
# "new run" form accepts a bare DOI, a doi.org link, or a Nature-family
# article URL (the same family the fixed corpus is drawn from) and extracts
# the DOI from whichever form was pasted.
_BARE_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_NATURE_ARTICLE_RE = re.compile(r"nature\.com/articles/([\w.\-]+)")


def _extract_doi(raw: str) -> str | None:
    raw = raw.strip()
    if not raw:
        return None
    if _BARE_DOI_RE.match(raw):
        return raw
    if "doi.org/" in raw:
        candidate = raw.split("doi.org/", 1)[1].strip("/")
        if _BARE_DOI_RE.match(candidate):
            return candidate
    m = _NATURE_ARTICLE_RE.search(raw)
    if m:
        return f"10.1038/{m.group(1)}"
    return None


# S8's own default (both model families, every flagged record plus a
# per-contract stratified sample) has no built-in cost ceiling -- appropriate
# for a CLI invocation someone is watching, not for a button in a UI that
# anyone could click against an arbitrary new paper of unknown size. The UI
# always runs S8 in this capped, single-cheap-model mode; the full check
# shape remains available via the CLI/Python API for a deliberate, budgeted
# run.
_UI_V3_MODELS = (ModelSpec(provider="anthropic", model="claude-haiku-4-5"),)
_UI_V3_MAX_RECORDS = 200


@app.get("/")
def index() -> RedirectResponse:
    return RedirectResponse(url="/runs")


def _elapsed(created_at: str, updated_at: str) -> str:
    """created_at/updated_at are now_iso() strings (ISO-8601)."""
    try:
        delta = datetime.fromisoformat(updated_at) - datetime.fromisoformat(created_at)
    except ValueError:
        return "—"
    total_s = int(delta.total_seconds())
    if total_s < 60:
        return f"{total_s}s"
    m, s = divmod(total_s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def _run_summary_row(conn: Any, run: dict[str, Any]) -> dict[str, Any]:
    """Augments one runs-list row with the stage-progress strip and
    classification-distribution bar, so the list shows records, review
    state and elapsed time at a glance.
    """
    reached = STAGE_ORDER.index(run["stage_reached"]) + 1 if run["stage_reached"] else 0
    counts = stage_counts(conn, run["id"])
    bucket_data = _build_buckets(conn, run["id"])
    total = bucket_data["total"]
    row = dict(run)
    row["stage_strip"] = [
        {"title": STAGE_LABELS[stage_id], "reached": i < reached}
        for i, stage_id in enumerate(STAGE_ORDER)
    ]
    row["distribution"] = [
        {"title": b["key"], "color": b["color"], "pct": (b["count"] / total * 100) if total else 0}
        for b in bucket_data["buckets"]
    ]
    row["records"] = counts["s6_project"].get("records")
    row["review"] = counts["s9_arbitrate"].get("needs_review")
    row["elapsed"] = _elapsed(run["created_at"], run["updated_at"])
    return row


@app.get("/runs")
def runs_index(request: Request, error: str = "") -> Response:
    engine = get_engine()
    with engine.connect() as conn:
        runs = [_run_summary_row(conn, r) for r in list_runs(conn)]
    return templates.TemplateResponse(
        request, "runs_list.html", {"runs": runs, "error": error}
    )


@app.post("/runs/new")
async def create_new_run(request: Request) -> Response:
    """Any paper with a resolvable DOI works, not just the fixed corpus --
    S0 Acquire (see s0_acquire.py) is DOI-driven from the start. Accepts a
    bare DOI, a doi.org link, or a Nature-family article URL.
    """
    form = await request.form()
    raw_input = str(form.get("doi_or_url", ""))
    paper_key = str(form.get("paper_key", "")).strip()

    doi = _extract_doi(raw_input)
    if doi is None:
        message = (
            f"Couldn't find a DOI in {raw_input.strip()!r} -- paste a DOI directly "
            "(e.g. 10.1038/s41586-021-04103-z), a doi.org link, or a Nature-family "
            "article URL."
        )
        return RedirectResponse(url=f"/runs?error={quote(message)}", status_code=303)

    if not paper_key:
        paper_key = doi.split("/")[-1]

    engine = get_engine()
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key=paper_key, doi=doi)
        event_store.emit(
            conn,
            run_id=run_id,
            event_type="stage_started",
            message=f"run created for {paper_key} ({doi}) via the UI",
            level="info",
        )
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


# Stages currently executing, tracked in-process so the rail can show
# "running" -- good enough for `uv run s7 ui`'s single-process dev server,
# which is this app's only deployment target.
_RUNNING_STAGES: set[tuple[str, str]] = set()


def _stage_status(counts: dict[str, int], *, running: bool) -> str:
    if running:
        return "running"
    if not counts:
        return "pending"
    if counts.get("failed"):
        return "failed"
    if counts.get("needs_review") or counts.get("rejected") or counts.get("v1_fail"):
        return "needs_review"
    return "done"


def _build_rail(
    conn: Any, run_id: str, *, selected_stage: str | None = None
) -> list[dict[str, Any]]:
    counts = stage_counts(conn, run_id)
    rail = []
    for stage_id in STAGE_ORDER:
        c = counts[stage_id]
        status = _stage_status(c, running=(run_id, stage_id) in _RUNNING_STAGES)
        headline = c.get(STAGE_HEADLINE_KEY[stage_id])
        rail.append(
            {
                "id": stage_id,
                "code": STAGE_CODES[stage_id],
                "label": STAGE_LABELS[stage_id],
                "short_label": STAGE_SHORT_LABELS[stage_id],
                "status": status,
                "counts": c,
                "headline_count": f"{headline:,}" if isinstance(headline, int) else None,
                "meter_pct": 100 if status in _STATUS_RAN else 0,
            }
        )
    return rail


def _spine_pct(rail: list[dict[str, Any]]) -> int:
    """How far down the spine the teal progress overlay reaches -- the index
    of the furthest stage that has actually run (any outcome, not just a
    clean one), as a fraction of the whole pipeline.
    """
    reached = [i for i, s in enumerate(rail) if s["status"] in _STATUS_RAN | {"running"}]
    if not reached:
        return 0
    return round((max(reached) + 1) / len(rail) * 100)


@app.get("/runs/{run_id}")
def run_detail(request: Request, run_id: str) -> Response:
    engine = get_engine()
    with engine.connect() as conn:
        run = get_run(conn, run_id)
        rail = _build_rail(conn, run_id)
    poll = any(r["status"] == "running" for r in rail)
    return templates.TemplateResponse(
        request,
        "run_detail.html",
        {"run": run, "run_id": run_id, "rail": rail, "poll": poll, "spine_pct": _spine_pct(rail)},
    )


@app.get("/runs/{run_id}/rail")
def run_rail(request: Request, run_id: str) -> Response:
    """Polled by the stage rail while any stage is running (see
    run_detail.html) so status/counts update without a full page reload.
    """
    engine = get_engine()
    with engine.connect() as conn:
        run = get_run(conn, run_id)
        rail = _build_rail(conn, run_id)
    any_running = any(status == "running" for status in (r["status"] for r in rail))
    return templates.TemplateResponse(
        request,
        "partials/stage_rail.html",
        {
            "run": run,
            "run_id": run_id,
            "rail": rail,
            "poll": any_running,
            "spine_pct": _spine_pct(rail),
        },
    )


async def _execute_stage(run_id: str, stage_id: str, *, force: bool) -> None:
    key = (run_id, stage_id)
    _RUNNING_STAGES.add(key)
    engine = get_engine()
    try:
        kwargs: dict[str, Any] = {"force": force}
        if stage_id == "s8_validate":
            kwargs["v3_models"] = _UI_V3_MODELS
            kwargs["v3_max_records"] = _UI_V3_MAX_RECORDS
        result = await STAGE_MODULES[stage_id].run(run_id, **kwargs)
        with engine.begin() as conn:
            event_store.emit(
                conn,
                run_id=run_id,
                stage=stage_id,
                event_type="stage_finished" if result.status == "done" else "error",
                message=f"{STAGE_LABELS[stage_id]} {result.status} (triggered from UI)",
                payload=result.counts,
            )
    except Exception as exc:  # noqa: BLE001 -- surfaced to the log drawer, not swallowed
        with engine.begin() as conn:
            event_store.emit(
                conn,
                run_id=run_id,
                stage=stage_id,
                event_type="error",
                level="error",
                message=f"{STAGE_LABELS[stage_id]} failed: {exc}",
            )
    finally:
        _RUNNING_STAGES.discard(key)


@app.post("/runs/{run_id}/stages/{stage_id}/run")
def trigger_stage(
    request: Request, run_id: str, stage_id: str, background_tasks: BackgroundTasks
) -> Response:
    """Fires the stage in the background and returns immediately, so the UI
    stays usable while a run is in progress. The rail's polling (see
    run_rail) picks up completion; the log drawer's SSE stream shows it
    happening in real time.
    """
    if stage_id not in STAGE_MODULES:
        return Response(status_code=404)
    if (run_id, stage_id) in _RUNNING_STAGES:
        return Response(status_code=409, content=b"already running")

    force = request.query_params.get("force") == "1"
    background_tasks.add_task(_execute_stage, run_id, stage_id, force=force)

    engine = get_engine()
    with engine.connect() as conn:
        run = get_run(conn, run_id)
        rail = _build_rail(conn, run_id)
    return templates.TemplateResponse(
        request,
        "partials/stage_rail.html",
        {"run": run, "run_id": run_id, "rail": rail, "poll": True, "spine_pct": _spine_pct(rail)},
    )


@app.get("/runs/{run_id}/stages/{stage_id}/panel")
def stage_panel(request: Request, run_id: str, stage_id: str) -> Response:
    if stage_id not in STAGE_LABELS:
        return Response(status_code=404)

    if stage_id in ARTIFACT_BROWSER_STAGES:
        engine = get_engine()
        with engine.connect() as conn:
            top_level = list_top_level(conn, run_id)
            tree = [
                {"artifact": a, "children": list_children(conn, a["id"])} for a in top_level
            ]
        return templates.TemplateResponse(
            request, "partials/artifact_browser.html", {"tree": tree}
        )

    if stage_id == "s2_parse":
        engine = get_engine()
        with engine.connect() as conn:
            summary = list_parsed_artifact_summary(conn, run_id)
        return templates.TemplateResponse(
            request, "partials/parse_artifact_list.html", {"run_id": run_id, "summary": summary}
        )

    if stage_id == "s3_classify":
        engine = get_engine()
        with engine.connect() as conn:
            items = list_classifications_for_run(conn, run_id)
            bucket_data = _build_buckets(conn, run_id)
            counts = stage_counts(conn, run_id)
        flow = {
            "n1": counts["s1_explode"].get("sheets"),
            "n2": counts["s2_parse"].get("tables"),
            "running": (run_id, "s3_classify") in _RUNNING_STAGES,
        }
        return templates.TemplateResponse(
            request,
            "partials/classify_panel.html",
            {
                "run_id": run_id,
                "items": items,
                "classification_ids": CLASSIFICATION_IDS,
                "flow": flow,
                **bucket_data,
            },
        )

    if stage_id == "s5_contract":
        engine = get_engine()
        with engine.connect() as conn:
            contracts = list_contracts_for_run(conn, run_id)
            groups_by_id: dict[str, list[dict[str, Any]]] = {}
            for c in contracts:
                groups_by_id.setdefault(c["agreement_group_id"], []).append(c)
            groups = [
                {
                    "agreement_group_id": gid,
                    "file_name": items[0]["file_name"],
                    "sheet_name": items[0]["sheet_name"],
                    "fragment_count": len(list_member_table_ids(conn, items[0]["id"])),
                    "row_entities": sorted({i["row_entity"] for i in items}),
                    "providers": [i["model_spec"] for i in items],
                    "min_confidence": min(i["overall_confidence"] for i in items),
                    "needs_review": any(i["needs_review"] for i in items),
                    # For a 2-provider group, needs_review IS the disagreement flag --
                    # s5_contract.py only sets it that way when disagree=True or when a
                    # single provider succeeded, and here len(items) == 2 rules out the
                    # latter. No re-comparison needed.
                    "disagreement": len(items) == 2 and any(i["needs_review"] for i in items),
                }
                for gid, items in groups_by_id.items()
            ]
        groups.sort(key=lambda g: (not g["needs_review"], g["file_name"], g["sheet_name"] or ""))
        return templates.TemplateResponse(
            request, "partials/contract_list.html", {"run_id": run_id, "groups": groups}
        )

    return templates.TemplateResponse(
        request,
        "partials/stage_not_built.html",
        {"stage_label": STAGE_LABELS[stage_id]},
    )


def _disagreeing_column_indices(
    mappings_a: list[dict[str, Any]], mappings_b: list[dict[str, Any]]
) -> set[int]:
    """Mirrors s5_contract._mappings_disagree's per-column comparison, but
    operating on the raw column_mappings rows already loaded for display
    rather than reconstructing ContractInduction objects just to diff them.
    """

    def targets_by_index(mappings: list[dict[str, Any]]) -> dict[int, frozenset[str | None]]:
        by_index: dict[int, set[str | None]] = {}
        for m in mappings:
            by_index.setdefault(m["source_column_index"], set()).add(m["target_field"])
        return {i: frozenset(v) for i, v in by_index.items()}

    a = targets_by_index(mappings_a)
    b = targets_by_index(mappings_b)
    empty: frozenset[str | None] = frozenset()
    return {i for i in set(a) | set(b) if a.get(i, empty) != b.get(i, empty)}


@app.get("/runs/{run_id}/contracts/{agreement_group_id}")
def contract_detail(request: Request, run_id: str, agreement_group_id: str) -> Response:
    engine = get_engine()
    with engine.connect() as conn:
        contracts = list_contracts_for_agreement_group(conn, agreement_group_id)
        if not contracts:
            return Response(status_code=404)
        for c in contracts:
            c["mappings"] = list_column_mappings(conn, c["id"])
        llm_calls = list_llm_calls_for_entity(conn, contracts[0]["parsed_table_id"])

    disagreeing_indices: set[int] = set()
    if len(contracts) == 2:
        disagreeing_indices = _disagreeing_column_indices(
            contracts[0]["mappings"], contracts[1]["mappings"]
        )

    return templates.TemplateResponse(
        request,
        "contract_detail.html",
        {
            "run_id": run_id,
            "contracts": contracts,
            "llm_calls": llm_calls,
            "disagreeing_indices": disagreeing_indices,
        },
    )


@app.get("/runs/{run_id}/artifacts/{artifact_id}/parsed")
def artifact_parsed_tables(request: Request, run_id: str, artifact_id: str) -> Response:
    engine = get_engine()
    with engine.connect() as conn:
        artifact = get_artifact(conn, artifact_id)
        tables = list_parsed_tables_for_artifact(conn, artifact_id)
    return templates.TemplateResponse(
        request,
        "parsed_tables_list.html",
        {"run_id": run_id, "artifact": artifact, "tables": tables},
    )


@app.get("/runs/{run_id}/parsed_tables/{parsed_table_id}")
def parsed_table_detail(request: Request, run_id: str, parsed_table_id: str) -> Response:
    engine = get_engine()
    with engine.connect() as conn:
        table = get_parsed_table(conn, parsed_table_id)
        if table is None:
            return Response(status_code=404)
        artifact = get_artifact(conn, table["artifact_id"])
        cells = list_parsed_cells(conn, parsed_table_id)

    source_grid = None
    if artifact is not None and artifact["kind"] == "sheet":
        sheet_rows = [c["sheet_row"] for c in cells if c["sheet_row"] is not None]
        start_row = row_window_for_cells(sheet_rows)
        source_grid = read_sheet_grid(str(artifact["storage_path"]), start_row=start_row)
    else:
        start_row = 0

    return templates.TemplateResponse(
        request,
        "parsed_table_detail.html",
        {
            "run_id": run_id,
            "table": table,
            "artifact": artifact,
            "cells": cells,
            "source_grid": source_grid,
            "source_start_row": start_row,
        },
    )


@app.post("/runs/{run_id}/classifications/{classification_id}/override")
async def override_classification(
    request: Request, run_id: str, classification_id: str
) -> Response:
    form = await request.form()
    override_class = str(form.get("override_class", "")).strip()
    if not override_class:
        return Response(status_code=400)

    engine = get_engine()
    with engine.begin() as conn:
        existing = get_classification(conn, classification_id)
        if existing is None:
            return Response(status_code=404)
        set_human_override(conn, classification_id, override_class)
        insert_human_label(
            conn,
            target_type="artifact_classification",
            target_id=existing["artifact_id"],
            field="classification_id",
            original_value=existing["classification_id"],
            corrected_value=override_class,
            action="correct",
        )
        updated = get_classification(conn, classification_id)

    return templates.TemplateResponse(
        request,
        "partials/classification_row.html",
        {"run_id": run_id, "c": updated, "classification_ids": CLASSIFICATION_IDS},
    )


@app.get("/runs/{run_id}/events/stream")
async def run_events_stream(run_id: str) -> EventSourceResponse:
    async def gen() -> AsyncGenerator[dict[str, str], None]:
        after_id = 0
        engine = get_engine()
        while True:
            with engine.connect() as conn:
                rows = event_store.since(conn, run_id, after_id=after_id)
            for row in rows:
                after_id = row["id"]
                yield {"event": "log", "data": json.dumps(row)}
            await asyncio.sleep(1.0)

    return EventSourceResponse(gen())
