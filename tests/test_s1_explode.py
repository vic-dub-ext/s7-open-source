import io

import openpyxl
import pytest

from s7.stages import s1_explode
from s7.storage import store_bytes
from s7.store.artifacts import insert_artifact, list_children
from s7.store.db import get_engine, now_iso
from s7.store.runs import create_run


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


def _xlsx_bytes(sheets: dict[str, list[list[object]]]) -> bytes:
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for row in rows:
            ws.append(row)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _make_top_level_artifact(engine, *, run_id: str, file_name: str, data: bytes, mime: str) -> str:
    from s7.config import get_settings

    settings = get_settings()
    settings.ensure_dirs()
    digest, path = store_bytes(settings.downloads_dir, data)
    with engine.begin() as conn:
        return insert_artifact(
            conn,
            run_id=run_id,
            kind="supplement",
            file_name=file_name,
            mime_type=mime,
            byte_size=len(data),
            sha256=digest,
            storage_path=str(path),
            retrieved_at=now_iso(),
        )


@pytest.mark.asyncio
async def test_explode_workbook_splits_sheets_and_flags_empty(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1/test")

    data = _xlsx_bytes(
        {
            "Table1": [
                ["gene", "trait", "p"],
                ["BRCA1", "cancer", "1e-8"],
                ["TP53", "cancer", "2e-9"],
            ],
            "Empty": [["only one cell"]],
        }
    )
    _make_top_level_artifact(
        engine,
        run_id=run_id,
        file_name="supp.xlsx",
        data=data,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    result = await s1_explode.run(run_id)
    assert result.status == "done"
    assert result.counts["sheets"] == 2
    assert result.counts["empty"] == 1

    with engine.begin() as conn:
        from s7.store.artifacts import list_top_level

        parent = list_top_level(conn, run_id)[0]
        children = {c["sheet_name"]: c for c in list_children(conn, parent["id"])}

    assert children["Table1"]["row_count"] == 3
    assert children["Table1"]["col_count"] == 3
    assert children["Table1"]["skip_reason"] is None

    assert children["Empty"]["skip_reason"] == "empty_sheet"


@pytest.mark.asyncio
async def test_explode_wraps_csv(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1/test")

    csv_bytes = b"gene,trait,p\nBRCA1,cancer,1e-8\nTP53,cancer,2e-9\n"
    _make_top_level_artifact(
        engine, run_id=run_id, file_name="supp.csv", data=csv_bytes, mime="text/csv"
    )

    result = await s1_explode.run(run_id)
    assert result.counts["sheets"] == 1

    with engine.begin() as conn:
        from s7.store.artifacts import list_top_level

        parent = list_top_level(conn, run_id)[0]
        children = list_children(conn, parent["id"])
    assert len(children) == 1
    assert children[0]["row_count"] == 3
    assert children[0]["col_count"] == 3


@pytest.mark.asyncio
async def test_explode_passes_through_pdf_unchanged(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1/test")

    _make_top_level_artifact(
        engine, run_id=run_id, file_name="supp.pdf", data=b"%PDF-1.4 fake", mime="application/pdf"
    )

    result = await s1_explode.run(run_id)
    assert result.counts["sheets"] == 0
    assert result.counts["passthrough"] == 1


@pytest.mark.asyncio
async def test_explode_is_idempotent_without_force(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1/test")

    data = _xlsx_bytes({"Table1": [["a", "b"], ["1", "2"]]})
    _make_top_level_artifact(
        engine,
        run_id=run_id,
        file_name="supp.xlsx",
        data=data,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    first = await s1_explode.run(run_id)
    assert first.status == "done"
    second = await s1_explode.run(run_id)
    assert second.status == "skipped"


@pytest.mark.asyncio
async def test_explode_with_force_replaces_rather_than_duplicates(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1/test")

    data = _xlsx_bytes({"Table1": [["a", "b"], ["1", "2"]]})
    _make_top_level_artifact(
        engine,
        run_id=run_id,
        file_name="supp.xlsx",
        data=data,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    await s1_explode.run(run_id)
    second = await s1_explode.run(run_id, force=True)
    assert second.status == "done"
    assert second.counts["sheets"] == 1

    with engine.begin() as conn:
        from s7.store.artifacts import list_top_level

        parent = list_top_level(conn, run_id)[0]
        children = list_children(conn, parent["id"])
    assert len(children) == 1


@pytest.mark.asyncio
async def test_large_sheet_is_flagged_as_classify_sample(db, monkeypatch) -> None:
    monkeypatch.setattr(s1_explode, "CLASSIFY_SAMPLE_ROW_CAP", 3)
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1/test")

    rows = [["h1", "h2"]] + [[str(i), str(i * 2)] for i in range(10)]
    data = _xlsx_bytes({"Big": rows})
    _make_top_level_artifact(
        engine,
        run_id=run_id,
        file_name="supp.xlsx",
        data=data,
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )

    await s1_explode.run(run_id)
    with engine.begin() as conn:
        from s7.store.artifacts import list_top_level

        parent = list_top_level(conn, run_id)[0]
        child = list_children(conn, parent["id"])[0]
    assert child["row_count"] == 11
    assert bool(child["is_classify_sample"]) is True
