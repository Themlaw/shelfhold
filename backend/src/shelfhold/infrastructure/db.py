"""SQLite engine in WAL mode (ADR-0004)."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, event


def create_db_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite+pysqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection: Any, _record: object) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA busy_timeout=5000")  # first: journal_mode itself may need the lock
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()

    return engine


class ForeignKeyViolationError(Exception):
    """A migration left rows that point nowhere."""

    def __init__(self, violations: list[tuple[Any, ...]]) -> None:
        super().__init__(f"foreign key violations after migration: {violations[:5]}")
        self.violations = violations


@contextmanager
def foreign_keys_disabled(connection: Connection) -> Iterator[None]:
    """`PRAGMA foreign_keys=OFF` for the whole migration, then back ON.

    SQLite ignores that pragma inside a transaction, so it is committed before the migration
    starts. Without it, a batch rebuild of a parent table (`DROP TABLE user`) cascades to its
    children. The caller checks `PRAGMA foreign_key_check` before leaving the context.
    """
    connection.commit()
    connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
    if connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() != 0:
        raise RuntimeError("could not disable foreign keys: a transaction is already open")
    try:
        yield
    finally:
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=ON")


def check_foreign_keys(connection: Connection) -> None:
    violations = [tuple(row) for row in connection.exec_driver_sql("PRAGMA foreign_key_check")]
    if violations:
        raise ForeignKeyViolationError(violations)
