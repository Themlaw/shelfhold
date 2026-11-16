"""Migration 0081 (M4 L04): `fs_intent.op` accepts `utime`.

The closed list of intent operations gains the change of a file's modification time; no row is
touched; the downgrade refuses while one is open."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config
from tests.integration.test_schema_migration_0046 import add_row

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-EXP-041")


def test_the_revision_is_0081_and_follows_0080() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0081"]
    assert script.get_revision("0081").down_revision == "0080"


def intent(db: sqlite3.Connection, op: str, state: str = "applying") -> None:
    add_row(db, "fs_intent", id=new_ulid(), op=op, intent_schema_version="1", state=state, epoch=0)


def test_utime_is_an_intent_after_the_upgrade_and_not_before(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), "0080")
    with (
        closing(sqlite3.connect(settings.database_path)) as db,
        pytest.raises(sqlite3.IntegrityError),
    ):
        intent(db, "utime")
    assert "0081" in migrate(settings).applied
    with closing(sqlite3.connect(settings.database_path)) as db:
        intent(db, "utime", "done")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            intent(db, "chmod")


def test_downgrade_refuses_an_open_utime_and_drops_the_closed_ones(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        intent(db, "utime", "applying")
        db.commit()
    with pytest.raises(RuntimeError, match="utime intent"):
        command.downgrade(alembic_config(settings.database_path), "0080")
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("UPDATE fs_intent SET state = 'done' WHERE op = 'utime'")
        db.commit()
    command.downgrade(alembic_config(settings.database_path), "0080")
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute("SELECT count(*) FROM fs_intent").fetchone()[0] == 0
