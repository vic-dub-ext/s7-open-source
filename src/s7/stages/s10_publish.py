"""S10 - Publish. Writes the Parquet dataset, a generated data_dictionary.md,
and coverage_report.json.

Like S7/S9, this stage doesn't own a dedicated output table -- its output is
files on disk -- so idempotency is checked via events.has_stage_finished
rather than row existence.

Routing:
  - auto_pass, human_confirmed, human_corrected -> main dataset
    (data/parquet/). A human_corrected record has already been fixed by a
    person, same as human_confirmed, so it belongs in the main dataset --
    leaving corrected records in neither dataset would itself be the kind
    of quiet data loss this pipeline exists to avoid.
    No run in this codebase has produced human_corrected records yet
    (S11's review UI isn't built), so this is currently a no-op distinction.
  - needs_review -> quarantine (data/quarantine/), same schema, kept
    separate so it's never accidentally mixed into the main dataset.
  - rejected -> excluded from both. Still counted in coverage_report.json
    ("how many... auto-passed" implies the honest denominator too).

Both datasets are Hive-partitioned by source_doi (pyarrow URL-encodes the
"/" in a DOI automatically -- verified against a real DOI before relying on
it).
"""

from __future__ import annotations

import json
import types
import typing
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any, get_args, get_origin

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from s7.config import get_settings
from s7.models.record import AssociationRecord
from s7.models.stage import StageResult, StageStatus
from s7.store import events
from s7.store.artifacts import list_top_level
from s7.store.checks import list_check_results_for_run
from s7.store.classifications import list_classifications_for_run
from s7.store.contracts import list_contracts_for_run
from s7.store.db import get_engine
from s7.store.parsed import list_parsed_tables_for_run
from s7.store.records import list_records_for_run
from s7.store.runs import get_run, update_run_status

STAGE = "s10_publish"

MAIN_DATASET_STATUSES = ("auto_pass", "human_confirmed", "human_corrected")
QUARANTINE_STATUSES = ("needs_review",)


def _record_to_row(record_row: dict[str, Any], checks: list[dict[str, Any]]) -> dict[str, Any]:
    """One association_records DB row -> a validated AssociationRecord dict,
    ready for pyarrow. `run_id` is an internal pipeline concept, not part of
    the published schema (source_doi + record_id already identify a row),
    so it's dropped rather than passed through.
    """
    payload = {k: v for k, v in record_row.items() if k != "run_id"}
    payload["strand_ambiguous"] = bool(record_row.get("strand_ambiguous") or False)
    payload["check_results"] = [
        {
            "check_name": c["check_name"],
            "status": c["status"],
            "detail": c["detail"],
            "checked_by": c["checked_by"],
        }
        for c in checks
    ]
    validated = AssociationRecord.model_validate(payload)
    return validated.model_dump(mode="python")


_CHECK_RESULT_ARROW_TYPE = pa.struct(
    [
        pa.field("check_name", pa.string()),
        pa.field("status", pa.string()),
        pa.field("detail", pa.string()),
        pa.field("checked_by", pa.string()),
    ]
)


def _arrow_type(annotation: Any) -> pa.DataType:
    """Maps one AssociationRecord field's Python type to a pyarrow type.
    Every field is nullable regardless of the mapping (see _arrow_schema) --
    this only decides *what type a value is when present*.

    Both spellings of a union have to be caught: Optional[X] / Union[X, None]
    report typing.Union, while PEP 604's `X | None` reports the distinct
    types.UnionType. Missing the latter sent every `int | None` and
    `float | None` field to the pa.string() fallback, publishing p_value,
    effect_value, n_total and friends as strings.
    """
    origin = get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        non_none = [a for a in get_args(annotation) if a is not type(None)]
        return _arrow_type(non_none[0]) if len(non_none) == 1 else pa.string()
    if origin is typing.Literal:
        return pa.string()
    if origin is list:
        return pa.list_(_CHECK_RESULT_ARROW_TYPE)
    if annotation is bool:
        return pa.bool_()
    if annotation is int:
        return pa.int64()
    if annotation is float:
        return pa.float64()
    if annotation is datetime:
        # now_iso() (store/db.py) always writes a UTC-aware ISO string, so
        # Pydantic parses extracted_at as a tz-aware datetime -- pyarrow
        # needs the tz named explicitly or it rejects a tz-aware value
        # against a naive timestamp type.
        return pa.timestamp("us", tz="UTC")
    return pa.string()


def _arrow_schema() -> pa.Schema:
    """Derived from AssociationRecord itself, once, so every partition file
    -- across every paper, every run, every S10 invocation -- shares
    exactly one schema. Without this, pyarrow infers a column's type from
    whatever's in that one call's batch of rows: a field that happens to be
    entirely None in one paper's records (e.g. chen2024depression has no
    resolved chrom/pos_b38/ref/alt at all) gets inferred as pyarrow's
    `null` type, while the same field has real string/float data in
    another paper's batch -- and reading the two partitions together as
    one dataset then fails with "Unsupported cast from string to null."
    Found live, publishing a second real paper alongside the first.
    """
    return pa.schema(
        [
            pa.field(name, _arrow_type(field.annotation), nullable=True)
            for name, field in AssociationRecord.model_fields.items()
        ]
    )


_SCHEMA = _arrow_schema()


def _write_dataset(root: Any, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    table = pa.Table.from_pylist(rows, schema=_SCHEMA)
    pq.write_to_dataset(
        table,
        root_path=str(root),
        partition_cols=["source_doi"],
        existing_data_behavior="delete_matching",
    )


def _format_type(annotation: Any) -> str:
    """A short, human-readable rendering of a Pydantic field's annotation
    for data_dictionary.md -- e.g. "increases | decreases | unknown | null"
    for an Optional[Literal[...]], "str" for a plain field.

    Catches both union spellings, for the same reason as _arrow_type above.
    """
    origin = get_origin(annotation)
    if origin is typing.Union or origin is types.UnionType:
        parts = [_format_type(a) for a in get_args(annotation) if a is not type(None)]
        nullable = type(None) in get_args(annotation)
        return " | ".join(parts) + (" | null" if nullable else "")
    if origin is typing.Literal:
        return " | ".join(repr(v) for v in get_args(annotation))
    if origin is list:
        (inner,) = get_args(annotation)
        return f"list[{_format_type(inner)}]"
    return getattr(annotation, "__name__", str(annotation))


def generate_data_dictionary() -> str:
    """Rendered straight from AssociationRecord.model_fields -- see the
    module docstring on models/record.py: this cannot drift from the schema
    because there is no second place the field list is written down.
    """
    lines = [
        "# S7 Data Dictionary",
        "",
        "Generated from `s7.models.record.AssociationRecord` -- do not hand-edit; "
        "edit the field definitions and re-run `s7 stage s10_publish` instead.",
        "",
        "| Field | Type | Required | Description |",
        "|---|---|---|---|",
    ]
    for name, field in AssociationRecord.model_fields.items():
        required = "yes" if field.is_required() else "no"
        description = (field.description or "").replace("|", "\\|")
        type_str = _format_type(field.annotation).replace("|", "\\|")
        lines.append(f"| `{name}` | {type_str} | {required} | {description} |")
    lines.append("")
    return "\n".join(lines)


def _build_coverage_entry(conn: Any, run_id: str) -> dict[str, Any]:
    run_row = get_run(conn, run_id)
    assert run_row is not None

    artifacts_found = len(list_top_level(conn, run_id))
    parsed_tables = list_parsed_tables_for_run(conn, run_id)
    artifacts_parsed = len({pt["artifact_id"] for pt in parsed_tables})

    classified_by_bucket: dict[str, int] = defaultdict(int)
    for c in list_classifications_for_run(conn, run_id):
        bucket = c["human_override_class"] or c["classification_id"]
        classified_by_bucket[bucket] += 1

    contracts_induced = len(list_contracts_for_run(conn, run_id))

    records = list_records_for_run(conn, run_id)
    by_status: dict[str, int] = defaultdict(int)
    for r in records:
        by_status[r["review_status"]] += 1

    checks = list_check_results_for_run(conn, run_id)
    v1_fail = sum(1 for c in checks if c["check_name"] == "v1_arithmetic" and c["status"] == "fail")
    v2_fail = sum(1 for c in checks if c["check_name"] == "v2_grounding" and c["status"] == "fail")
    v3_evaluated = len({c["record_id"] for c in checks if c["check_name"].startswith("v3_")})

    return {
        "paper_key": run_row["paper_key"],
        "run_id": run_id,
        "artifacts_found": artifacts_found,
        "artifacts_parsed": artifacts_parsed,
        "classified_by_bucket": dict(classified_by_bucket),
        "contracts_induced": contracts_induced,
        "records_projected": len(records),
        "records_by_review_status": dict(by_status),
        "v1_arithmetic_failures": v1_fail,
        "v2_grounding_failures": v2_fail,
        "v3_records_evaluated": v3_evaluated,
        "published_main_dataset": sum(by_status.get(s, 0) for s in MAIN_DATASET_STATUSES),
        "published_quarantine": sum(by_status.get(s, 0) for s in QUARANTINE_STATUSES),
        "excluded_rejected": by_status.get("rejected", 0),
    }


async def run(run_id: str, *, force: bool = False) -> StageResult:
    settings = get_settings()
    settings.ensure_dirs()
    engine = get_engine()

    with engine.begin() as conn:
        run_row = get_run(conn, run_id)
        if run_row is None:
            raise ValueError(f"no such run: {run_id}")

        already = events.has_stage_finished(conn, run_id, STAGE)
        if not force and already:
            return StageResult(stage=STAGE, status="skipped", counts={})

        events.emit(
            conn, run_id=run_id, stage=STAGE, event_type="stage_started", message="S10 started"
        )
        update_run_status(conn, run_id, status="running", stage_reached=STAGE)

        records = list_records_for_run(conn, run_id)
        checks = list_check_results_for_run(conn, run_id)
        checks_by_record: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for c in checks:
            checks_by_record[c["record_id"]].append(c)

        main_rows, quarantine_rows = [], []
        failed_validation: list[dict[str, Any]] = []
        for r in records:
            try:
                row = _record_to_row(r, checks_by_record.get(r["record_id"], []))
            except ValidationError as exc:
                # A record that fails AssociationRecord's own schema is
                # exactly the kind of thing S10 must not silently publish --
                # but one corrupt row (e.g. an upstream column-mapping bug)
                # shouldn't take down the whole run either. Excluded from
                # both datasets, counted honestly in the coverage report.
                failed_validation.append({"record_id": r["record_id"], "error": str(exc)})
                continue
            if r["review_status"] in MAIN_DATASET_STATUSES:
                main_rows.append(row)
            elif r["review_status"] in QUARANTINE_STATUSES:
                quarantine_rows.append(row)
            # rejected: excluded from both.

        if failed_validation:
            events.emit(
                conn,
                run_id=run_id,
                stage=STAGE,
                event_type="validation_failed",
                level="warn",
                message=f"{len(failed_validation)} records failed AssociationRecord "
                "validation and were excluded from publication",
                payload={"record_ids": [f["record_id"] for f in failed_validation][:20]},
            )

        _write_dataset(settings.parquet_dir, main_rows)
        _write_dataset(settings.quarantine_dir, quarantine_rows)

        settings.data_dictionary_path.write_text(generate_data_dictionary())

        coverage_entry = _build_coverage_entry(conn, run_id)
        coverage_entry["excluded_failed_validation"] = len(failed_validation)
        report: dict[str, Any] = {"generated_at": datetime.now(UTC).isoformat(), "papers": {}}
        if settings.coverage_report_path.exists():
            existing = json.loads(settings.coverage_report_path.read_text())
            report["papers"] = existing.get("papers", {})
        report["papers"][run_row["doi"]] = coverage_entry
        settings.coverage_report_path.write_text(json.dumps(report, indent=2))

        counts = {
            "records_published_main": len(main_rows),
            "records_published_quarantine": len(quarantine_rows),
            "records_excluded_rejected": coverage_entry["excluded_rejected"],
            "records_excluded_failed_validation": len(failed_validation),
        }

        status: StageStatus = "done"
        update_run_status(conn, run_id, status=status, stage_reached=STAGE)
        events.emit(
            conn,
            run_id=run_id,
            stage=STAGE,
            event_type="stage_finished",
            message=f"S10 publish finished: {counts['records_published_main']} published, "
            f"{counts['records_published_quarantine']} quarantined, "
            f"{counts['records_excluded_rejected']} rejected, "
            f"{counts['records_excluded_failed_validation']} failed validation",
            payload=counts,
        )

    return StageResult(stage=STAGE, status=status, counts=counts)
