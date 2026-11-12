"""Migration 0056 (M3 L09, EF-USR-016): `user.visibility`, nullable, what a profile's requests and
activity show to the others. NULL is "the default of the role": no row is touched, a child stays
`household` and an adult `private` without anyone writing anything."""

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

pytestmark = pytest.mark.req("EF-USR-016", "ENF-OPS-002")


def _previous() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0056").down_revision
    assert isinstance(down, str)
    return down


def _columns(db: sqlite3.Connection) -> dict[str, tuple[object, ...]]:
    return {r[1]: (r[2], r[3], r[4]) for r in db.execute('PRAGMA table_info("user")')}


def test_upgrade_adds_a_nullable_column_and_downgrade_removes_it(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO user (id, username, role, can_rate) "
            "VALUES ('01J00000000000000000000001', 'mum', 'member', 0)"
        )
        db.commit()
        assert "visibility" not in _columns(db)
    assert "0056" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        assert _columns(db)["visibility"] == ("VARCHAR", 0, None)
        assert db.execute("SELECT visibility FROM user").fetchone() == (None,)  # untouched
    command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert "visibility" not in _columns(db)
        assert db.execute("SELECT username FROM user").fetchone() == ("mum",)


def test_migrated_column_equals_create_all(settings: Settings, tmp_path: Path) -> None:
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
        assert _columns(a)["visibility"] == _columns(b)["visibility"]
