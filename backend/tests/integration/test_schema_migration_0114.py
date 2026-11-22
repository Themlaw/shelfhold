"""Migration 0114 (M6 L23a, EF-INT-005): `webhook_subscription.rotated_at`.

The previous secret stays in the sealed document (no new envelope: the export exclusion, the
write guard and the key rotation keep covering it); the column says when the rotation happened,
and a delivery signs with both secrets for 24 h after it. Reversible: the downgrade drops the
column, and a rotated subscription then signs with its current secret only, which every receiver
that moved over already has."""

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


def test_the_revision_is_0114_the_head_on_0108() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0114"]  # the head is pinned here, by the newest
    assert script.get_revision("0114").down_revision == HEAD_BEFORE


def test_the_column_comes_and_goes_without_touching_the_rows(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), HEAD_BEFORE)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO webhook_subscription (id, url_host, event_types, last_event_id) "
            "VALUES (?, 'hooks.example.org', '[\"unit.stored\"]', 0)",
            (SUBSCRIPTION,),
        )
    assert "0114" in migrate(settings).applied
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        assert "rotated_at" in _columns(database)
        conn.execute(
            "UPDATE webhook_subscription SET rotated_at = '2026-11-22 09:00:00' "
            "WHERE id = ?",
            (SUBSCRIPTION,),
        )
    command.downgrade(alembic_config(database), HEAD_BEFORE)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        assert "rotated_at" not in _columns(database)
        assert conn.execute(
            "SELECT url_host, last_event_id FROM webhook_subscription"
        ).fetchall() == [("hooks.example.org", 0)]
