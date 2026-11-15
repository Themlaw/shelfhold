"""Migration 0065 (M4 L14a): the view `reading_progress` is replaced by the merge."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config


@pytest.mark.req("ENF-OPS-002", "EF-LEC-014")
def test_single_alembic_head_with_0065_on_0071() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0065"]
    assert script.get_revision("0065").down_revision == "0071"


@pytest.mark.req("ENF-OPS-002", "EF-LEC-014")
def test_upgrade_replaces_the_view_and_downgrade_restores_the_old_one(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0071")
    with closing(sqlite3.connect(database)) as conn:
        before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'reading_progress'"
        ).fetchone()[0]
        assert "min(" not in before.lower()
    assert "0065" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as conn:
        after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'reading_progress'"
        ).fetchone()[0]
        assert "min(" in after.lower()  # `client_ts` bounded by `server_ts`
        assert conn.execute("SELECT * FROM reading_progress").fetchall() == []
    command.downgrade(alembic_config(database), "0071")
    with closing(sqlite3.connect(database)) as conn:
        again = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'reading_progress'"
        ).fetchone()[0]
    assert again == before
