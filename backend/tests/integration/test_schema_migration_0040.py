"""Migration 0040 (M2 L29, EF-SRC-038): `source_binding.last_refreshed_at`, the instant the source
was last asked to refresh the series. Nullable, no row is touched, the DDL is what `create_all`
makes."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from shelfhold.infrastructure.schema import metadata
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("EF-SRC-038", "ENF-OPS-002")


def _previous() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0040").down_revision
    assert isinstance(down, str)
    return down


def _info(db: sqlite3.Connection, table: str) -> dict[str, tuple[object, ...]]:
    return {r[1]: (r[2], r[3], r[4], r[5]) for r in db.execute(f"PRAGMA table_info({table})")}


def test_upgrade_adds_the_column_and_downgrade_removes_it(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert "last_refreshed_at" not in _info(db, "source_binding")
    assert "0040" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        assert "last_refreshed_at" in _info(db, "source_binding")
    command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert "last_refreshed_at" not in _info(db, "source_binding")


def test_migrated_column_equals_create_all(settings: Settings, tmp_path: Path) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    fresh = tmp_path / "fresh.db"
    engine = sa.create_engine(f"sqlite:///{fresh}")
    metadata.create_all(engine)
    engine.dispose()
    with (
        closing(sqlite3.connect(settings.database_path)) as a,
        closing(sqlite3.connect(fresh)) as b,
    ):
        migrated, created = _info(a, "source_binding"), _info(b, "source_binding")
    assert migrated["last_refreshed_at"] == created["last_refreshed_at"]
    assert created["last_refreshed_at"][1] == 0  # nullable
