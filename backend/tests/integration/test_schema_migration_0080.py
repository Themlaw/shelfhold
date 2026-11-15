"""Migration 0080 (M4 L17): what was pushed of each profile's read state to a Suwayomi series.

One table, `read_state_push_note`, per owner profile, binding and chapter: the state last pushed
(`read` or `unread`) and when. It is the cursor of the push: the diff against the view is what is
sent. It goes with the profile and with the binding; reversible.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables
from tests.integration.test_schema_migration_0046 import add_row

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-SRC-026")


def test_the_revision_is_0080_and_follows_the_head_of_main() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0080"]
    assert script.get_revision("0080").down_revision in {"0078", "0079"}


def test_upgrade_adds_the_table_and_downgrade_drops_it(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), "0078")
    assert "read_state_push_note" not in tables(settings.database_path)
    assert "0080" in migrate(settings).applied
    assert "read_state_push_note" in tables(settings.database_path)
    command.downgrade(alembic_config(settings.database_path), "0078")
    assert "read_state_push_note" not in tables(settings.database_path)


def test_a_state_is_read_or_unread_and_a_chapter_is_noted_once_per_owner(
    settings: Settings,
) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    user, binding, unit = new_ulid(), new_ulid(), new_ulid()
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("PRAGMA foreign_keys = OFF")
        add_row(db, "read_state_push_note", user_id=user, source_binding_id=binding,
                remote_unit_id="k:/s/1", unit_id=unit, state="read",
                pushed_at="2026-11-15 12:00:00")  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError):
            add_row(db, "read_state_push_note", user_id=user, source_binding_id=binding,
                    remote_unit_id="k:/s/1", unit_id=unit, state="unread",
                    pushed_at="2026-11-15 12:00:00")  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError):
            add_row(db, "read_state_push_note", user_id=user, source_binding_id=binding,
                    remote_unit_id="k:/s/2", unit_id=unit, state="half",
                    pushed_at="2026-11-15 12:00:00")  # fmt: skip
