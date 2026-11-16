"""Migration 0082 (M4 L49b): the slice of a series block.

One table, `bundle_setting`, one row per Edition that has a slice: the number of chapters a part
may hold (1 to 256). It goes with its Edition; reversible; nothing existing is touched.
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

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-INT-007")


def test_the_revision_is_0082_and_follows_the_head_of_main() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0082"]
    assert script.get_revision("0082").down_revision == "0081"


def test_upgrade_adds_the_table_and_downgrade_drops_it(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), "0081")
    assert "bundle_setting" not in tables(settings.database_path)
    assert "0082" in migrate(settings).applied
    assert "bundle_setting" in tables(settings.database_path)
    command.downgrade(alembic_config(settings.database_path), "0081")
    assert "bundle_setting" not in tables(settings.database_path)


def test_a_slice_is_between_1_and_256_and_an_edition_has_one(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    edition = new_ulid()
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("PRAGMA foreign_keys = OFF")
        add_row(db, "bundle_setting", edition_id=edition, slice_chapters=256,
                updated_at="2026-11-16 12:00:00")  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError):
            add_row(db, "bundle_setting", edition_id=edition, slice_chapters=3,
                    updated_at="2026-11-16 12:00:00")  # fmt: skip
        for bad in (0, 257, -1):
            with pytest.raises(sqlite3.IntegrityError):
                add_row(db, "bundle_setting", edition_id=new_ulid(), slice_chapters=bad,
                        updated_at="2026-11-16 12:00:00")  # fmt: skip
