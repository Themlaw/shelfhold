"""Migration 0021 (M2 L16, EF-EXP-016): `export_upgrade`, one row per same-path replacement."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("EF-EXP-016", "ENF-OPS-002")

ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
SHA = "sha256:" + "a" * 64


def previous_revision() -> str:
    """The head of main when 0021 lands: the test follows `down_revision`, not a number."""
    down = ScriptDirectory(str(default_script_location())).get_revision("0021").down_revision
    assert isinstance(down, str)
    return down


def test_upgrade_adds_export_upgrade_and_downgrade_removes_it(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), previous_revision())
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'a', 'admin', 1)", (ID,)
        )
        db.commit()

    result = migrate(settings)

    assert "0021" in result.applied
    assert "export_upgrade" in tables(database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT username FROM user").fetchall() == [("a",)]
        with pytest.raises(sqlite3.IntegrityError):  # a reason outside the closed list
            db.execute(
                "INSERT INTO export_upgrade (id, intent_id, shelf_destination_id, export_item_id, "
                "unit_id, reason, old_file_hash, new_file_hash) VALUES (?, ?, ?, ?, ?, "
                "'revision', ?, ?)",
                (ID, ID, ID, ID, ID, SHA, SHA),
            )
        with pytest.raises(sqlite3.IntegrityError):  # `restored` needs its instant
            db.execute(
                "INSERT INTO export_upgrade (id, intent_id, shelf_destination_id, export_item_id, "
                "unit_id, reason, old_file_hash, new_file_hash, state) VALUES (?, ?, ?, ?, ?, "
                "'restore', ?, ?, 'restored')",
                (ID, ID, ID, ID, ID, SHA, SHA),
            )

    command.downgrade(alembic_config(database), previous_revision())
    assert "export_upgrade" not in tables(database)
    assert current_revision(database) == previous_revision()
