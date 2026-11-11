"""Migration 0016 (M1b L19, EF-SRC-029): the table of the requests to follow a series."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

USER = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
REQUEST = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


@pytest.mark.req("EF-SRC-029", "ENF-OPS-002")
def test_upgrade_from_0015_adds_the_table_and_keeps_data(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0015")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'a', 'admin', 1)", (USER,)
        )
        db.commit()
    assert current_revision(database) == "0015"

    result = migrate(settings)

    assert result.applied == ("0016", "0017", "0020", "0022")
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT username FROM user").fetchall() == [("a",)]
        db.execute(
            "INSERT INTO follow_request (id, source_key, remote_id) VALUES (?, 'suwayomi:1', '/a')",
            (REQUEST,),
        )
        row = db.execute("SELECT state, visibility, requester_user_id FROM follow_request")
        assert row.fetchall() == [("approved", "ops", None)]  # the defaults
        with pytest.raises(sqlite3.IntegrityError):  # one request per series
            db.execute(
                "INSERT INTO follow_request (id, source_key, remote_id) "
                "VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAX', 'suwayomi:1', '/a')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO follow_request (id, source_key, remote_id, state) "
                "VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAY', 'suwayomi:1', '/b', 'done')"
            )
