"""Migration 0093 (M4 L18b, EF-EXP-025): what a collection needs to be pushed to Kavita.

`collection.public_approved_at`, and `collection_remote` rebuilt with a `note` and no foreign key to
`collection` (the note must outlive the collection). A row made before the migration is kept and
reads as having nothing noted. The head of the migrations is pinned here, by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-EXP-025")

COLLECTION = "01HZZZZZZZZZZZZZZZZZZZZZZA"
TARGET = "01HZZZZZZZZZZZZZZZZZZZZZZB"


def _columns(database: object, table: str) -> set[str]:
    with closing(sqlite3.connect(str(database))) as conn:
        return {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}


def test_the_revision_is_0093_on_0092() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # one head, whichever later lot moved it
    assert script.get_revision("0093").down_revision == "0092"


def test_the_columns_come_and_go_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0092")
    assert "public_approved_at" not in _columns(database, "collection")
    assert "note" not in _columns(database, "collection_remote")
    assert "0093" in migrate(settings).applied
    assert "public_approved_at" in _columns(database, "collection")
    assert "note" in _columns(database, "collection_remote")
    command.downgrade(alembic_config(database), "0092")
    assert "public_approved_at" not in _columns(database, "collection")
    assert "note" not in _columns(database, "collection_remote")


def test_a_row_made_before_is_kept_and_outlives_its_collection(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0092")
    with closing(sqlite3.connect(str(database), isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO export_target (id, kind, name) VALUES (?, 'kavita', 'k')", (TARGET,)
        )
        conn.execute(
            "INSERT INTO collection (id, kind, name) VALUES (?, 'collection', 'Old')", (COLLECTION,)
        )
        conn.execute(
            "INSERT INTO collection_remote (collection_id, target_id, remote_id) "
            "VALUES (?, ?, '1')",
            (COLLECTION, TARGET),
        )
    migrate(settings)
    with closing(sqlite3.connect(str(database), isolation_level=None)) as conn:
        assert conn.execute("SELECT public_approved_at FROM collection").fetchone() == (None,)
        assert conn.execute("SELECT note, remote_id FROM collection_remote").fetchone() == (
            "{}",
            "1",
        )
        # deleting the collection leaves what was noted of it (a withdrawal is due)
        conn.execute("DELETE FROM collection")
        assert conn.execute("SELECT count(*) FROM collection_remote").fetchone() == (1,)
