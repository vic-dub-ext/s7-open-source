import zipfile
from io import BytesIO

import httpx
import pytest

from s7.stages import s0_acquire
from s7.store.artifacts import list_top_level
from s7.store.db import get_engine
from s7.store.runs import create_run


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


def _suppl_zip() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("41586_2021_0000_MOESM1_ESM.xlsx", b"fake workbook bytes")
        zf.writestr("41586_2021_0000_Fig1_HTML.gif", b"fake figure bytes")
    return buf.getvalue()


def _mock_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/search"):
            return httpx.Response(
                200, json={"resultList": {"result": [{"pmcid": "PMC1234567"}]}}
            )
        if path.endswith("/fullTextXML"):
            return httpx.Response(200, content=b"<article/>")
        if path.endswith("/supplementaryFiles"):
            return httpx.Response(
                200, content=_suppl_zip(), headers={"content-type": "application/zip"}
            )
        return httpx.Response(404)

    return httpx.MockTransport(handler)


@pytest.mark.asyncio
async def test_acquire_fetches_article_and_supplement(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1038/test")

    result = await s0_acquire.run(run_id, transport=_mock_transport())
    assert result.status == "done"
    assert result.counts["found"] == 2  # article + 1 kept MOESM entry (figure dropped)

    with engine.begin() as conn:
        top = list_top_level(conn, run_id)
    kinds = sorted(a["kind"] for a in top)
    assert kinds == ["article", "supplement"]


@pytest.mark.asyncio
async def test_acquire_is_idempotent_without_force(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1038/test")

    first = await s0_acquire.run(run_id, transport=_mock_transport())
    assert first.status == "done"
    second = await s0_acquire.run(run_id, transport=_mock_transport())
    assert second.status == "skipped"


@pytest.mark.asyncio
async def test_acquire_with_force_replaces_rather_than_duplicates(db) -> None:
    engine = db
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1038/test")

    await s0_acquire.run(run_id, transport=_mock_transport())
    second = await s0_acquire.run(run_id, force=True, transport=_mock_transport())
    assert second.status == "done"
    assert second.counts["found"] == 2

    with engine.begin() as conn:
        top = list_top_level(conn, run_id)
    assert len(top) == 2
