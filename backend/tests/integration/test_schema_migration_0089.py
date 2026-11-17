"""Migration 0089 (M4 L10, EF-EXP-019, EF-BIB-044): `export_assembly`, `fs_intent.op = assemble`.

One table that goes with its destination and its volume; the closed list of intent operations gains
`assemble`; no row is touched; reversible, and the downgrade refuses while an assembly is open. The
head of the migrations is pinned by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables
from tests.integration.test_schema_migration_0046 import add_row

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-EXP-019", "EF-BIB-044")


def test_the_revision_is_0089_and_follows_0088() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # the head is pinned by the newest migration test
    assert script.get_revision("0089").down_revision == "0088"


def intent(db: sqlite3.Connection, op: str, state: str = "applying") -> None:
    add_row(db, "fs_intent", id=new_ulid(), op=op, intent_schema_version="1", state=state, epoch=0)


def test_the_table_and_the_intent_come_and_go_with_the_migration(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), "0088")
    assert "export_assembly" not in tables(settings.database_path)
    with (
        closing(sqlite3.connect(settings.database_path)) as db,
        pytest.raises(sqlite3.IntegrityError),
    ):
        intent(db, "assemble")
    assert "0089" in migrate(settings).applied
    assert "export_assembly" in tables(settings.database_path)
    with closing(sqlite3.connect(settings.database_path)) as db:
        intent(db, "assemble", "done")
        db.commit()
        with pytest.raises(sqlite3.IntegrityError):
            intent(db, "chmod")
    command.downgrade(alembic_config(settings.database_path), "0088")
    assert "export_assembly" not in tables(settings.database_path)


def test_downgrade_refuses_an_open_assembly(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        intent(db, "assemble", "applying")
        db.commit()
    with pytest.raises(RuntimeError, match="assemble intent"):
        command.downgrade(alembic_config(settings.database_path), "0088")
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("UPDATE fs_intent SET state = 'done' WHERE op = 'assemble'")
        db.commit()
    command.downgrade(alembic_config(settings.database_path), "0088")
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute("SELECT count(*) FROM fs_intent").fetchone()[0] == 0


def test_an_assembly_names_a_volume_and_a_destination_that_exist(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):  # a volume and a destination that exist
            add_row(
                db, "export_assembly", id=new_ulid(), intent_id=new_ulid(),
                shelf_destination_id=new_ulid(), export_item_id=new_ulid(), volume="1",
                old_items="[]", derived_hash="sha256:" + "a" * 64, file_hash="sha256:" + "b" * 64,
            )  # fmt: skip
