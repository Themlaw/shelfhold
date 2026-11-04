"""SQLite engine in WAL mode (ADR-0004)."""

from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event


def create_db_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection: Any, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine
