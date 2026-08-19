"""SQLAlchemy Core engine and schema init. Core, not the ORM -- the
inspection UI wants explicit SQL it can show verbatim, not query-builder
magic.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, text

from s7.config import Settings

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def new_id() -> str:
    return str(uuid.uuid4())


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def make_engine(db_path: Path) -> Engine:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", future=True)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Idempotent: safe to call on every startup."""
    schema_sql = _SCHEMA_PATH.read_text()
    with engine.begin() as conn:
        for statement in schema_sql.split(";"):
            statement = statement.strip()
            if statement:
                conn.execute(text(statement))


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = Settings()
    settings.ensure_dirs()
    engine = make_engine(settings.db_path)
    init_db(engine)
    return engine
