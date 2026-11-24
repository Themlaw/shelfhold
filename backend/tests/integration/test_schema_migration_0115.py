"""Migration 0115 (M6 L17a, EF-INT-012): `kobo_sync_point`, the state of one device's sync.

One row per device: what its last completed sync gave it (the entitlements and the tags, with the
fingerprint of each book), and when that was. The next sync diffs the catalogue against it, so a
device hears only of what changed since it was last seen. Created from the schema so the DDL stays
identical to `metadata.create_all` (ENF-OPS-002); nothing existing is touched; reversible: the
downgrade drops the table, and every device then syncs as if for the first time.
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

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-INT-012")

HEAD_BEFORE = "0113"
USER = new_ulid()
DEVICE = new_ulid()


def _seed_device(database: str) -> None:
    """A profile and its device: the row the sync point is keyed by."""
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            'INSERT INTO "user" (id, username, role, created_at) '
            "VALUES (?, 'reader', 'member', '2026-11-01 10:00:00')",
            (USER,),
        )
        conn.execute(
            "INSERT INTO device (id, subject_user_id, kind, created_at) "
            "VALUES (?, ?, 'kobo', '2026-11-02 10:00:00')",
            (DEVICE, USER),
        )


def test_the_revision_is_0115_the_head_on_0113() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # one head, whichever later lot moved it
    assert script.get_revision("0115").down_revision == HEAD_BEFORE


def test_the_table_is_created_and_the_way_back_drops_it(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), HEAD_BEFORE)
    assert "kobo_sync_point" not in tables(database)

    assert "0115" in migrate(settings).applied
    assert "kobo_sync_point" in tables(database)
    _seed_device(database)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO kobo_sync_point (id, device_id, synced_at, books, tags) "
            "VALUES (?, ?, '2026-11-23 09:00:00', '{}', '{}')",
            (new_ulid(), DEVICE),
        )
        assert conn.execute("SELECT count(*) FROM kobo_sync_point").fetchall() == [(1,)]

    command.downgrade(alembic_config(database), HEAD_BEFORE)
    assert "kobo_sync_point" not in tables(database)


def test_a_database_created_fresh_runs_the_migration_once(settings: Settings) -> None:
    """A database created after the schema gained the table builds it by `migrate` itself: the
    migration is the one that creates it either way, and running it again changes nothing."""
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    result = migrate(settings)
    assert result.applied[-1] == "0115"
    assert "kobo_sync_point" in tables(settings.database_path)
    assert migrate(settings).applied == ()
