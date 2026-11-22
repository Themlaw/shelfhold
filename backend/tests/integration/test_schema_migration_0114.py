"""Migration 0114 (M6 L23a, EF-INT-005): `webhook_subscription.rotated_at`.

The previous secret stays in the sealed document; the column says when the rotation happened, and
a delivery signs with both secrets for 24 h after it. 0096 builds the table from the schema
itself, so the migration adds the column only where it is missing: a database created before the
schema gained it. Reversible: the downgrade drops the column, and a rotated subscription then
signs with its current secret only, which every receiver that moved over already has."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-INT-005")

HEAD_BEFORE = "0108"
SUBSCRIPTION = "01HZZZZZZZZZZZZZZZZZZZZZZA"


def _columns(database: str) -> list[str]:
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        return [row[1] for row in conn.execute("PRAGMA table_info(webhook_subscription)")]


def _insert_subscription(database: str) -> None:
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO webhook_subscription (id, url_host, event_types, last_event_id) "
            "VALUES (?, 'hooks.example.org', '[\"unit.stored\"]', 0)",
            (SUBSCRIPTION,),
        )


def test_the_revision_is_0114_the_head_on_0108() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0114"]  # the head is pinned here, by the newest
    assert script.get_revision("0114").down_revision == HEAD_BEFORE


def test_the_column_is_added_where_it_is_missing(settings: Settings) -> None:
    """A database created before the schema gained the column: 0114 brings it, and the way back
    drops it without touching the rows."""
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), HEAD_BEFORE)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        # what a database of 0108 and earlier looks like: no `rotated_at`
        conn.execute("ALTER TABLE webhook_subscription DROP COLUMN rotated_at")
    _insert_subscription(database)
    assert "rotated_at" not in _columns(database)

    assert "0114" in migrate(settings).applied
    assert "rotated_at" in _columns(database)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            "UPDATE webhook_subscription SET rotated_at = '2026-11-22 09:00:00' WHERE id = ?",
            (SUBSCRIPTION,),
        )
    command.downgrade(alembic_config(database), HEAD_BEFORE)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        assert "rotated_at" not in _columns(database)
        assert conn.execute(
            "SELECT url_host, last_event_id FROM webhook_subscription"
        ).fetchall() == [("hooks.example.org", 0)]


def test_a_database_created_fresh_needs_nothing_from_the_migration(settings: Settings) -> None:
    """0096 builds the table from the schema itself: a database created after the schema gained
    the column already has it, and 0114 changes nothing (the DDL of the two paths is the same)."""
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), HEAD_BEFORE)
    assert "rotated_at" in _columns(database)  # built by 0096, from the schema
    _insert_subscription(database)
    assert "0114" in migrate(settings).applied
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        assert conn.execute(
            "SELECT count(*) FROM webhook_subscription WHERE rotated_at IS NULL"
        ).fetchall() == [(1,)]
