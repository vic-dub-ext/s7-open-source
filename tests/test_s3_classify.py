import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from s7.config import get_settings
from s7.stages import s3_classify
from s7.storage import store_bytes
from s7.store.artifacts import insert_artifact
from s7.store.classifications import list_classifications_for_run
from s7.store.db import get_engine, now_iso
from s7.store.runs import create_run


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    monkeypatch.setenv("EXTEND_API_KEY", "test-key")
    get_engine.cache_clear()
    engine = get_engine()
    yield engine
    get_engine.cache_clear()


def _fake_run(
    *,
    status="PROCESSED",
    classification_id="assoc_gene_level",
    confidence=0.9,
    reasoning="because",
    credits=0.3,
    failure_reason=None,
):
    output = None
    if status == "PROCESSED":
        output = SimpleNamespace(
            id=classification_id,
            confidence=confidence,
            insights=[SimpleNamespace(content=reasoning)],
        )
    usage = SimpleNamespace(credits=credits)
    return SimpleNamespace(
        status=status, output=output, usage=usage, failure_reason=failure_reason, id="clr_test"
    )


@pytest.mark.asyncio
async def test_classify_live_retries_on_low_confidence() -> None:
    low = _fake_run(confidence=0.5)
    high = _fake_run(confidence=0.95)
    client = AsyncMock()
    client.create_classify_run.side_effect = ["run1", "run2"]
    client.wait_for_classify_run.side_effect = [low, high]

    run, retried = await s3_classify._classify_live(client, file_id="file_1")

    assert retried is True
    assert run.output.confidence == 0.95
    assert client.create_classify_run.call_count == 2
    _, first_kwargs = client.create_classify_run.call_args_list[0]
    _, second_kwargs = client.create_classify_run.call_args_list[1]
    assert first_kwargs["config"]["base_processor"] == s3_classify.BASE_PROCESSOR_FIRST_PASS
    assert second_kwargs["config"]["base_processor"] == s3_classify.BASE_PROCESSOR_RETRY


@pytest.mark.asyncio
async def test_classify_live_does_not_retry_on_high_confidence() -> None:
    high = _fake_run(confidence=0.95)
    client = AsyncMock()
    client.create_classify_run.side_effect = ["run1"]
    client.wait_for_classify_run.side_effect = [high]

    run, retried = await s3_classify._classify_live(client, file_id="file_1")

    assert retried is False
    assert client.create_classify_run.call_count == 1


def _make_artifact(engine, *, run_id: str, sha256_seed: bytes) -> dict:
    settings = get_settings()
    settings.ensure_dirs()
    digest, path = store_bytes(settings.downloads_dir, sha256_seed)
    with engine.begin() as conn:
        artifact_id = insert_artifact(
            conn,
            run_id=run_id,
            kind="sheet",
            file_name="Table1.xlsx",
            mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            byte_size=len(sha256_seed),
            sha256=digest,
            storage_path=str(path),
            retrieved_at=now_iso(),
            parent_artifact_id=None,
            sheet_name="Table1",
        )
    from s7.store.artifacts import get_artifact

    with engine.begin() as conn:
        return get_artifact(conn, artifact_id)


@pytest.mark.asyncio
async def test_classify_one_caches_across_artifacts_sharing_content(db) -> None:
    """Two different artifacts (e.g. from two different runs) that happen to
    share file content must only bill Extend once.
    """
    engine = db
    settings = get_settings()
    with engine.begin() as conn:
        run_id = create_run(conn, paper_key="test", doi="10.1/test")

    artifact_a = _make_artifact(engine, run_id=run_id, sha256_seed=b"identical content")
    artifact_b = _make_artifact(engine, run_id=run_id, sha256_seed=b"identical content")
    assert artifact_a["sha256"] == artifact_b["sha256"]

    client = AsyncMock()
    client.upload_file.return_value = SimpleNamespace(id="file_shared")
    client.create_classify_run.return_value = "clr_1"
    client.wait_for_classify_run.return_value = _fake_run(confidence=0.9)

    counts = {"classified": 0, "failed": 0, "needs_review": 0, "retried": 0, "cached": 0}
    lock = asyncio.Lock()

    await s3_classify._classify_one(
        engine,
        client,
        settings,
        run_id=run_id,
        artifact=artifact_a,
        counts=counts,
        counts_lock=lock,
    )
    await s3_classify._classify_one(
        engine,
        client,
        settings,
        run_id=run_id,
        artifact=artifact_b,
        counts=counts,
        counts_lock=lock,
    )

    assert counts["classified"] == 2
    assert counts["cached"] == 1  # second call was a cache hit
    assert client.create_classify_run.call_count == 1  # only billed once

    with engine.begin() as conn:
        rows = list_classifications_for_run(conn, run_id)
    assert len(rows) == 2
    assert {r["artifact_id"] for r in rows} == {artifact_a["id"], artifact_b["id"]}
