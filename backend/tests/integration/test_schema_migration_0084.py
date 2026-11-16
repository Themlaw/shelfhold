"""Migration 0084 (M4 L15, EF-BIB-026): `reading_progress` marks a transposed position
`approximate`, and the Units it journals as read.

The view holds no data and its columns do not change (frozen since 1.1): only what it says of a
position written by a change of preferred Edition does. Reversible. The head of the migrations is
pinned here, by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-026")


def test_the_revision_is_0084_and_the_only_head() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0084"]
    assert script.get_revision("0084").down_revision == "0083"


def _view_columns(settings: Settings) -> list[str]:
    with closing(sqlite3.connect(settings.database_path)) as db:
        return [r[1] for r in db.execute("PRAGMA table_info(reading_progress)")]


def _view_sql(settings: Settings) -> str:
    with closing(sqlite3.connect(settings.database_path)) as db:
        row = db.execute("SELECT sql FROM sqlite_master WHERE name = 'reading_progress'").fetchone()
        return str(row[0])


def test_the_view_changes_its_rule_and_not_its_columns_both_ways(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), "0083")
    before_columns, before_sql = _view_columns(settings), _view_sql(settings)
    assert "transposed" not in before_sql
    assert "0084" in migrate(settings).applied
    assert _view_columns(settings) == before_columns  # frozen since 1.1
    assert "transposed" in _view_sql(settings)
    command.downgrade(alembic_config(settings.database_path), "0083")
    assert _view_sql(settings) == before_sql
