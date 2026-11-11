"""Migration 0026 (M2 L12): external identifiers append-only in the database, and the journal of
attaches by external identifier (`edition_attach`), which "Séparer" reads to undo one exactly."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-003", "EF-BIB-055")

ID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


def previous_revision() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0026").down_revision
    assert isinstance(down, str)
    return down


def test_upgrade_keeps_external_ids_and_makes_them_append_only(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), previous_revision())
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO external_id (entity_type, entity_id, provider, value, origin) "
            "VALUES ('work', ?, 'anilist', '1', 'source')",
            (ID,),
        )
        db.commit()

    assert migrate(settings).applied[-1] == "0026"

    assert "edition_attach" in tables(database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT entity_id, state FROM external_id").fetchall() == [
            (ID, "current")
        ]
        for statement, params in (
            ("UPDATE external_id SET value = '2'", ()),
            ("UPDATE external_id SET entity_id = ?", (OTHER,)),
            ("DELETE FROM external_id", ()),
            (
                "INSERT OR REPLACE INTO external_id (entity_type, entity_id, provider, value, "
                "origin) VALUES ('work', ?, 'anilist', '1', 'source')",
                (OTHER,),
            ),
        ):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(statement, params)
        db.execute("UPDATE external_id SET state = 'superseded', origin = 'legacy'")
        assert db.execute("SELECT origin, state FROM external_id").fetchall() == [
            ("legacy", "superseded")
        ]


def test_downgrade_drops_the_journal_and_the_guards(settings: Settings) -> None:
    database = settings.database_path
    migrate(settings)
    command.downgrade(alembic_config(database), previous_revision())
    assert "edition_attach" not in tables(database)
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO external_id (entity_type, entity_id, provider, value, origin) "
            "VALUES ('work', ?, 'anilist', '1', 'source')",
            (ID,),
        )
        db.execute("DELETE FROM external_id")  # as before 0026
