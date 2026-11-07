"""Migration 0014 (D19, EF-USR-016): visibility and requester of a download request."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, migrate
from tests.integration.test_schema_migration_0002 import alembic_config


@pytest.mark.req("EF-USR-016", "ENF-OPS-002")
def test_upgrade_from_0013_keeps_data(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0013")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'a', 'admin', 1)",
            ("01ARZ3NDEKTSV4RRFFQ69G5FAV",),
        )
        db.commit()
    assert current_revision(database) == "0013"

    result = migrate(settings)

    assert result.applied[0] == "0014"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT username FROM user").fetchall() == [("a",)]
        columns = {row[1]: row for row in db.execute("PRAGMA table_info(download_request)")}
        assert {"visibility", "requester_user_id"} <= set(columns)
        assert columns["visibility"][3] == 0 and columns["requester_user_id"][3] == 0  # nullable
