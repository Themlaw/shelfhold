"""Migration 0060 (M3 L08, EF-USR-013): what a deletion of a user must reach.

Two triggers on the ticket tables (a `requester_user_id` that names no user becomes NULL) and the
one rewrite that `tombstone_lost_kid` allows (`actor_id` takes a `deleted:` token)."""

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


def _triggers(db: sqlite3.Connection) -> set[str]:
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")}


def test_upgrade_adds_the_ticket_triggers_and_downgrade_restores_the_closed_waiver(
    settings: Settings,
) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert "follow_request_requester_exists" not in _triggers(db)
        before = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'tombstone_lost_kid_no_update'"
        ).fetchone()[0]
        assert "WHEN" not in before
    assert "0060" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        names = _triggers(db)
        assert {"follow_request_requester_exists", "download_request_requester_exists"} <= names
        after = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'tombstone_lost_kid_no_update'"
        ).fetchone()[0]
        assert "deleted:%" in after
        db.execute(
            "INSERT INTO follow_request (id, source_key, remote_id, state, visibility, "
            "requester_user_id) VALUES ('01J00000000000000000000001', 'source-a', 'r', "
            "'approved', 'private', '01J00000000000000000000009')"
        )
        assert db.execute(
            "SELECT requester_user_id, visibility FROM follow_request"
        ).fetchone() == (None, "private")
    command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        names = _triggers(db)
        assert "follow_request_requester_exists" not in names
        assert "download_request_requester_exists" not in names
        restored = db.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'tombstone_lost_kid_no_update'"
        ).fetchone()[0]
        assert restored == before
