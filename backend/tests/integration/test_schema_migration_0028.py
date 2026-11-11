"""Migration 0028 (M2 L17, EF-BIB-043): what a pending revision asks the Kavita API to set."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("EF-BIB-043", "ENF-OPS-002")

ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def previous_revision() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0028").down_revision
    assert isinstance(down, str)
    return down


def _columns(db: sqlite3.Connection) -> list[str]:
    return [r[1] for r in db.execute("PRAGMA table_info(revision_pending)")]


def test_upgrade_adds_the_columns_and_downgrade_removes_them(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), previous_revision())
    with closing(sqlite3.connect(database)) as db:
        assert "applied_via" not in _columns(db)

    assert "0028" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        assert {"field", "value", "applied_via"} <= set(_columns(db))

    command.downgrade(alembic_config(database), previous_revision())
    with closing(sqlite3.connect(database)) as db:
        assert "applied_via" not in _columns(db)
    assert current_revision(database) == previous_revision()
