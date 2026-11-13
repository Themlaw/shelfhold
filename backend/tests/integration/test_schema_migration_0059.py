"""Migration 0059 (M3 L11, EF-INT-001): what `?since=` needs of the database.

`work.created_seq` (a trigger sets it from the sequence of `event` and moves the sequence by one,
so that no token can equal it) and `instance.events_purged_through` (what the retention purged)."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("EF-INT-001", "ENF-OPS-002")


def _previous() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0059").down_revision
    assert isinstance(down, str)
    return down


def _columns(db: sqlite3.Connection, table: str) -> dict[str, tuple[object, ...]]:
    return {r[1]: (r[2], r[3], r[4]) for r in db.execute(f'PRAGMA table_info("{table}")')}


def _work(db: sqlite3.Connection, work_id: str) -> None:
    db.execute(
        "INSERT INTO work (id, kind, title, sort_title) "
        "SELECT ?, key, 't', 't' FROM work_kind LIMIT 1",
        (work_id,),
    )


def test_upgrade_adds_both_columns_with_a_zero_default_and_downgrade_removes_them(
    settings: Settings,
) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        _work(db, "01J00000000000000000000001")
        db.commit()
        assert "created_seq" not in _columns(db, "work")
    assert "0059" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        assert _columns(db, "work")["created_seq"] == ("INTEGER", 1, "'0'")
        assert _columns(db, "instance")["events_purged_through"] == ("INTEGER", 1, "'0'")
        # a Work that existed before is older than any token
        assert db.execute("SELECT created_seq FROM work").fetchone() == (0,)
    command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert "created_seq" not in _columns(db, "work")
        assert "events_purged_through" not in _columns(db, "instance")
        assert db.execute("SELECT count(*) FROM work").fetchone() == (1,)
        trigger = db.execute("SELECT name FROM sqlite_master WHERE name = 'work_created_seq'")
        assert trigger.fetchone() is None


def test_each_new_work_moves_the_sequence_and_takes_the_new_value(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        seq = "SELECT COALESCE((SELECT seq FROM sqlite_sequence WHERE name = 'event'), 0)"
        start = db.execute(seq).fetchone()[0]
        _work(db, "01J00000000000000000000002")
        _work(db, "01J00000000000000000000003")
        db.commit()
        rows = dict(db.execute("SELECT id, created_seq FROM work ORDER BY created_seq"))
        assert rows == {
            "01J00000000000000000000002": start + 1,
            "01J00000000000000000000003": start + 2,
        }
        assert db.execute(seq).fetchone()[0] == start + 2
        db.execute(
            "INSERT INTO event (type, schema_version, payload, occurred_at, visibility) "
            "VALUES ('ops.test', 1, '{}', '2026-01-01 00:00:00', 'ops')"
        )
        assert db.execute("SELECT max(id) FROM event").fetchone()[0] == start + 3  # no id reused
