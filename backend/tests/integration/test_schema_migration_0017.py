"""Migration 0017 (M1b L16, EF-BIB-042 (4)): the one-row table of what "Mettre à l'abri" left."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

USER = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


@pytest.mark.req("EF-BIB-042", "ENF-OPS-002")
def test_upgrade_from_0016_adds_the_table_and_keeps_data(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0016")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'a', 'admin', 1)", (USER,)
        )
        db.commit()
    assert current_revision(database) == "0016"

    result = migrate(settings)

    assert result.applied[:3] == ("0017", "0020", "0025")  # later ones follow
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT username FROM user").fetchall() == [("a",)]
        db.execute("INSERT INTO safeguard_backlog (name, units, bytes) VALUES ('store', 3, 40)")
        row = db.execute("SELECT name, units, bytes, since FROM safeguard_backlog").fetchall()
        assert row == [("store", 3, 40, None)]
        with pytest.raises(sqlite3.IntegrityError):  # one queue only
            db.execute("INSERT INTO safeguard_backlog (name) VALUES ('other')")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE safeguard_backlog SET units = -1")
