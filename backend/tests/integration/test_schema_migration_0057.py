"""Migration 0057 (M3 L35, EF-USR-010): what an approval records on a request, and the memo of a
remote result. Nothing is rewritten; the migrated table equals `create_all`."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from shelfhold.infrastructure.schema import metadata
from tests.integration.test_schema_migration_0002 import alembic_config

NEW = {
    "visibility", "work_id", "edition_id", "shelf_id", "decided_by", "decided_at",
    "approved_age", "approved_x", "arrived_at", "started_at", "shown_title",
}  # fmt: skip
USER = "01J00000000000000000000001"


def _previous() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0057").down_revision
    assert isinstance(down, str)
    return down


def _columns(db: sqlite3.Connection, table: str) -> dict[str, tuple[object, ...]]:
    return {r[1]: (r[2], r[3], r[4]) for r in db.execute(f'PRAGMA table_info("{table}")')}


@pytest.mark.req("EF-USR-010")
def test_upgrade_adds_columns_and_keeps_rows_and_downgrade_removes_them(
    settings: Settings,
) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'mum', 'member', 0)",
            (USER,),
        )
        db.execute(
            "INSERT INTO request (id, requester_user_id, target_kind, target_value) "
            "VALUES ('01J00000000000000000000002', ?, 'free_text', 'old')",
            (USER,),
        )
        db.commit()
        assert not NEW & set(_columns(db, "request"))
        assert "memo" not in _columns(db, "pending_source_item")
    assert "0057" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        columns = _columns(db, "request")
        assert set(columns) >= NEW and "memo" in _columns(db, "pending_source_item")
        # a row made before reads as private and undecided
        row = db.execute(
            "SELECT visibility, work_id, decided_at, approved_age, approved_x FROM request"
        ).fetchone()
        assert row == ("private", None, None, None, 0)
    command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert not NEW & set(_columns(db, "request"))
        assert db.execute("SELECT target_value FROM request").fetchone() == ("old",)


@pytest.mark.req("EF-USR-010")
def test_migrated_columns_equal_create_all(settings: Settings, tmp_path: Path) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    fresh = tmp_path / "fresh.db"
    engine = sa.create_engine(f"sqlite:///{fresh}")
    metadata.create_all(engine)
    engine.dispose()
    with (
        closing(sqlite3.connect(settings.database_path)) as a,
        closing(sqlite3.connect(fresh)) as b,
    ):
        for table in ("request", "pending_source_item"):
            assert _columns(a, table) == _columns(b, table), table
