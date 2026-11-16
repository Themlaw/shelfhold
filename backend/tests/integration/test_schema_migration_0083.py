"""Migration 0083 (M4 L50, ADR-0053 §2): `bundle_member.page_map_state` learns `verified`, and the
column `kavita_pages` records the count Kavita announced at the last comparison.

The table is rebuilt (a CHECK is widened): its rows, its index and the trigger of `export_item`
must come out whole. Reversible; the DDL parity of the whole database is
`test_migrated_ddl_equals_create_all`.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-LEC-014")


def test_the_revision_is_0083_and_is_the_only_head() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0083"]
    assert script.get_revision("0083").down_revision == "0082"


def _columns(settings: Settings) -> list[str]:
    with closing(sqlite3.connect(settings.database_path)) as db:
        return [r[1] for r in db.execute("PRAGMA table_info(bundle_member)")]


def test_upgrade_adds_the_column_and_downgrade_drops_it(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), "0082")
    assert "kavita_pages" not in _columns(settings)
    assert "0083" in migrate(settings).applied
    assert "kavita_pages" in _columns(settings)
    command.downgrade(alembic_config(settings.database_path), "0082")
    assert "kavita_pages" not in _columns(settings)


def test_the_index_and_the_trigger_of_the_member_survive_both_ways(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    for target in ("0083", "0082", "0083"):
        if target == "0082":
            command.downgrade(alembic_config(settings.database_path), target)
        else:
            command.upgrade(alembic_config(settings.database_path), target)
        with closing(sqlite3.connect(settings.database_path)) as db:
            names = {r[0] for r in db.execute("SELECT name FROM sqlite_master")}
        assert {"ix_bundle_member_item", "export_item_wipe_bundle_members"} <= names


def test_a_state_is_one_of_three_and_a_page_count_is_not_negative(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as db:
        db.execute("PRAGMA foreign_keys = OFF")
        base = (
            "INSERT INTO bundle_member (version_id, rank, chapter_label, first_page, page_count, "
            "page_map_state, kavita_pages) VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZA', 1, 'c', 1, 3, ?, ?)"
        )
        db.execute(base, ("verified", 3))
        db.execute("DELETE FROM bundle_member")
        for state, pages in (("guessed", None), ("verified", -1)):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(base, (state, pages))
