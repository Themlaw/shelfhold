"""Migration 0062 (M3 L24, EF-BIB-020): the partial index the cover filter of a thumbnail uses."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("EF-BIB-020", "ENF-OPS-002")

INDEX = "ix_metadata_field_edition_cover"


def _indexes(db: sqlite3.Connection) -> set[str]:
    return {r[1] for r in db.execute('PRAGMA index_list("metadata_field")')}


def test_upgrade_adds_the_partial_index_and_downgrade_drops_it(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    down = ScriptDirectory(str(default_script_location())).get_revision("0062").down_revision
    assert down == "0059"
    command.upgrade(alembic_config(database), "0059")
    with closing(sqlite3.connect(database)) as db:
        assert INDEX not in _indexes(db)
    assert "0062" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        assert INDEX in _indexes(db)
        plan = db.execute(
            "EXPLAIN QUERY PLAN SELECT 1 FROM metadata_field WHERE entity_type = 'edition' "
            "AND field = 'cover' AND value = ?",
            ('"covers/x.webp"',),
        ).fetchall()
        assert INDEX in str(plan)  # the planner takes it: the lookup is a seek
    command.downgrade(alembic_config(database), "0059")
    with closing(sqlite3.connect(database)) as db:
        assert INDEX not in _indexes(db)
