"""Migration 0060 (M3 L08, EF-USR-013): what a deletion of a user must reach.

The one rewrite that `tombstone_lost_kid` allows (`actor_id` takes a `deleted:` token). No trigger
names the `user` table: it would break the batch rebuild of that table."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("EF-USR-013", "ENF-OPS-002")


def _previous() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0060").down_revision
    assert isinstance(down, str)
    return down


def test_upgrade_opens_the_one_rewrite_and_downgrade_closes_it_again(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), _previous())
    sql = "SELECT sql FROM sqlite_master WHERE name = 'tombstone_lost_kid_no_update'"
    with closing(sqlite3.connect(database)) as db:
        before = db.execute(sql).fetchone()[0]
        assert "WHEN" not in before
    assert "0060" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        assert "deleted:%" in db.execute(sql).fetchone()[0]
        mentioning_user = db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'trigger' AND sql LIKE '%\"user\"%'"
        ).fetchall()
        assert mentioning_user == []  # a trigger on `user` would break its batch rebuild
    command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert db.execute(sql).fetchone()[0] == before
