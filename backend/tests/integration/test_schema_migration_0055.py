"""Migration 0055 (M3 L25, EF-BIB-021): `trash_item.deletion_id` and `trash_item.snapshot`, both
nullable, what a restore from the bin needs. The bin entries of before keep NULL and are not
restorable; no row is touched, and the DDL is what `create_all` makes."""

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

pytestmark = pytest.mark.req("EF-BIB-021", "ENF-OPS-002")


def _previous() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0055").down_revision
    assert isinstance(down, str)
    return down


def _info(db: sqlite3.Connection) -> dict[str, tuple[object, ...]]:
    return {r[1]: (r[2], r[3], r[4], r[5]) for r in db.execute("PRAGMA table_info(trash_item)")}


def test_upgrade_adds_the_columns_and_downgrade_removes_them(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO trash_item (id, kind, ref, path, deleted_at, purge_after) VALUES "
            "('01J00000000000000000000001', 'unit', '01J00000000000000000000002', 'a/b', "
            "'2026-11-01 00:00:00', '2026-12-01 00:00:00')"
        )
        db.commit()
        assert "snapshot" not in _info(db)
    assert "0055" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        assert {"deletion_id", "snapshot"} <= set(_info(db))
        row = db.execute("SELECT deletion_id, snapshot, level FROM trash_item").fetchone()
        assert row == (None, None, "delete_content")  # the entry of before: not restorable
        db.execute(
            "INSERT INTO fs_intent (id, op, intent_schema_version, state, epoch) VALUES "
            "('01J00000000000000000000003', 'unbin', '1', 'done', 0)"
        )  # the restore's own intent is allowed now
        db.commit()
    command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert not {"deletion_id", "snapshot"} & set(_info(db))
        assert db.execute("SELECT count(*) FROM trash_item").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM fs_intent WHERE op = 'unbin'").fetchone() == (0,)


def test_downgrade_refuses_while_a_restore_intent_is_open(settings: Settings) -> None:
    """An open `unbin` intent is a restore whose files are still in a bin: the downgrade says so
    and changes nothing; once the intent is done it goes through."""
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    assert "0055" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO fs_intent (id, op, intent_schema_version, state, epoch) VALUES "
            "('01J00000000000000000000004', 'unbin', '1', 'applying', 0)"
        )
        db.commit()
    with pytest.raises(RuntimeError, match="restore intent"):
        command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT state FROM fs_intent WHERE op = 'unbin'").fetchall() == [
            ("applying",)
        ]
        assert {"deletion_id", "snapshot"} <= set(_info(db))  # nothing was undone
        db.execute("UPDATE fs_intent SET state = 'done' WHERE op = 'unbin'")
        db.commit()
    command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        assert not {"deletion_id", "snapshot"} & set(_info(db))


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
        migrated, created = _info(a), _info(b)
        indexes = [{r[1] for r in db.execute("PRAGMA index_list(trash_item)")} for db in (a, b)]
    for column in ("deletion_id", "snapshot"):
        assert migrated[column] == created[column]
        assert created[column][1] == 0  # nullable
    assert "ix_trash_item_deletion" in indexes[0] and "ix_trash_item_deletion" in indexes[1]
