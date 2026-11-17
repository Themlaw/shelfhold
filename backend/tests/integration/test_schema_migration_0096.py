"""Migration 0096 (M4 L24, EF-INT-005): the webhook subscriptions and their outbox.

Two tables, nothing else touched, reversible. The head of the migrations is pinned here, by the
newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-INT-005")

TABLES = {"webhook_subscription", "webhook_outbox"}


def _tables(database: object) -> set[str]:
    with closing(sqlite3.connect(str(database))) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in rows}


def test_the_revision_is_0096_on_0095() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_revision("0096").down_revision == "0095"


def test_the_tables_come_and_go_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0095")
    assert not TABLES & _tables(database)
    assert "0096" in migrate(settings).applied
    assert _tables(database) >= TABLES
    command.downgrade(alembic_config(database), "0095")
    assert not TABLES & _tables(database)


def test_one_delivery_per_subscription_event_and_version(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(str(settings.database_path), isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        row = (
            "INSERT INTO webhook_outbox (id, subscription_id, event_id, schema_version, "
            "next_try_at) VALUES (?, '01HZZZZZZZZZZZZZZZZZZZZZZA', 1, 1, '2026-11-17 09:00:00')"
        )
        conn.execute(row, ("01HZZZZZZZZZZZZZZZZZZZZZZB",))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(row, ("01HZZZZZZZZZZZZZZZZZZZZZZC",))
