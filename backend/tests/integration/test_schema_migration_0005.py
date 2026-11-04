"""Migration 0005 (job_view.window_end, job_view.cursor): upgrade keeps rows, downgrade is real."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command

from tests.integration.test_schema_migration_0002 import alembic_config

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
OTHER = "01ARZ3NDEKTSV4RRFFQ69G5FAW"


def columns(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as db:
        return {row[1] for row in db.execute("PRAGMA table_info(job_view)")}


@pytest.mark.req("ENF-OPS-014")
def test_upgrade_adds_the_columns_and_keeps_rows_and_indexes(tmp_path: Path) -> None:
    database = tmp_path / "a.db"
    config = alembic_config(database)
    command.upgrade(config, "0004")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO job_view (id, kind, status, dedupe_key) VALUES (?, 'k', 'queued', 'd')",
            (ULID,),
        )
        db.commit()
    assert "cursor" not in columns(database)

    command.upgrade(config, "0005")

    assert {"window_end", "cursor"} <= columns(database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT id, window_end, cursor FROM job_view").fetchall() == [
            (ULID, None, None)
        ]
        db.execute("UPDATE job_view SET cursor = '{\"next_index\": 3}'")
        with pytest.raises(sqlite3.IntegrityError, match="cursor_json"):
            db.execute("UPDATE job_view SET cursor = '{not json'")
        with pytest.raises(sqlite3.IntegrityError):  # the partial unique index survived
            db.execute(
                "INSERT INTO job_view (id, kind, status, dedupe_key) "
                "VALUES (?, 'k', 'queued', 'd')",
                (OTHER,),
            )
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.req("ENF-OPS-014")
def test_downgrade_removes_exactly_the_two_columns(tmp_path: Path) -> None:
    database = tmp_path / "d.db"
    config = alembic_config(database)
    command.upgrade(config, "0004")
    before = columns(database)
    command.upgrade(config, "0005")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO job_view (id, kind, status, cursor) "
            "VALUES (?, 'k', 'queued', '{\"next_index\": 1}')",
            (ULID,),
        )
        db.commit()

    command.downgrade(config, "0004")

    assert columns(database) == before
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT id FROM job_view").fetchall() == [(ULID,)]
        names = {r[1] for r in db.execute("PRAGMA index_list(job_view)")}
        assert "uq_job_view_dedupe_active" in names
