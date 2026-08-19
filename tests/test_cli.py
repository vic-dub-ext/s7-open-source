import pytest
from typer.testing import CliRunner

from s7.cli import app
from s7.store.db import get_engine

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    """The CLI reaches for the real Settings()/get_engine() singletons, so
    every test here must redirect them at an isolated DB -- otherwise it
    reads and writes the developer's actual ./data/s7.db.
    """
    monkeypatch.setenv("S7_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("S7_DB_PATH", str(tmp_path / "s7.db"))
    get_engine.cache_clear()
    yield
    get_engine.cache_clear()


def test_corpus_list_shows_all_papers() -> None:
    result = runner.invoke(app, ["corpus", "list"])
    assert result.exit_code == 0
    expected_keys = (
        "backman2021",
        "wang2021",
        "vanhout2020",
        "chen2024depression",
        "chen2023cognitive",
    )
    for key in expected_keys:
        assert key in result.stdout


def test_publish_with_no_ready_runs_reports_clean_error() -> None:
    # Every stage module is implemented, so the CLI's
    # NotImplementedError-catching path in `stage` has no remaining
    # not-yet-built stage to exercise it against; this covers the other
    # still-current "nothing to do yet" path instead: `publish` with no run
    # that has reached s9_arbitrate.
    result = runner.invoke(app, ["publish"])
    assert result.exit_code == 1
    assert "s9_arbitrate" in result.stdout


def test_stage_of_unknown_run_reports_clean_error() -> None:
    result = runner.invoke(app, ["stage", "s0_acquire", "--run", "does-not-exist"])
    assert result.exit_code == 1
    assert "does-not-exist" in result.stdout
