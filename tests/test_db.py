from pathlib import Path

from sqlalchemy import text

from s7.store.db import init_db, make_engine


def test_init_db_creates_all_tables(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table'")
        ).fetchall()
    tables = {r[0] for r in rows}
    for expected in (
        "runs",
        "artifacts",
        "parsed_tables",
        "parsed_cells",
        "sheet_classifications",
        "methods_bundles",
        "schema_contracts",
        "column_mappings",
        "association_records",
        "check_results",
        "human_labels",
        "llm_calls",
        "provider_calls",
        "ontology_cache",
        "events",
    ):
        assert expected in tables


def test_init_db_is_idempotent(tmp_path: Path) -> None:
    engine = make_engine(tmp_path / "test.db")
    init_db(engine)
    init_db(engine)  # must not raise
