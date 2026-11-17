"""Migration 0086 (M4 L18a, EF-BIB-016): `collection`, `collection_item`, `collection_remote`.

Three tables that go with their owner, their list and their Work; nothing else is touched;
reversible. The head of the migrations is pinned by the newest one (0087 now).
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-016")

NEW = {"collection", "collection_item", "collection_remote"}
USER = "01HZZZZZZZZZZZZZZZZZZZZZZA"
LIST = "01HZZZZZZZZZZZZZZZZZZZZZZB"
WORK = "01HZZZZZZZZZZZZZZZZZZZZZZC"
ITEM = "01HZZZZZZZZZZZZZZZZZZZZZZD"


def test_the_revision_is_0086_on_0085() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_revision("0086").down_revision == "0085"


def test_the_tables_come_and_go_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0085")
    assert not NEW & tables(database)
    assert "0086" in migrate(settings).applied
    assert tables(database) >= NEW
    command.downgrade(alembic_config(database), "0085")
    assert not NEW & tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_a_collection_has_a_known_kind_and_a_name(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        for kind, name in (("playlist", "x"), ("collection", ""), ("collection", "x" * 300)):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO collection (id, kind, name) VALUES (?, ?, ?)", (LIST, kind, name)
                )
        conn.execute(
            "INSERT INTO collection (id, kind, name) VALUES (?, 'reading_list', 'x')", (LIST,)
        )
        # no owner: the household's; a profile's own list names its owner (and goes with it)
        assert (
            conn.execute("SELECT owner_id FROM collection WHERE id = ?", (LIST,)).fetchone()[0]
            is None
        )
        with pytest.raises(sqlite3.IntegrityError):  # the owner must exist
            conn.execute(
                "INSERT INTO collection (id, kind, name, owner_id) "
                "VALUES (?, 'collection', 'y', ?)",
                (ITEM, USER),
            )


def test_an_item_needs_its_list_and_its_work_and_a_free_position(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        kind = conn.execute("SELECT key FROM work_kind LIMIT 1").fetchone()[0]
        conn.execute(
            "INSERT INTO work (id, kind, title, sort_title) VALUES (?, ?, 'T', 't')", (WORK, kind)
        )
        conn.execute(
            "INSERT INTO collection (id, kind, name) VALUES (?, 'collection', 'x')", (LIST,)
        )
        add = (
            "INSERT INTO collection_item (id, collection_id, position, work_id) VALUES (?, ?, ?, ?)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (ITEM, LIST, -1, WORK))  # never negative
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (ITEM, LIST, 0, LIST))  # a Work that does not exist
        conn.execute(add, (ITEM, LIST, 0, WORK))
        with pytest.raises(sqlite3.IntegrityError):  # one place per position
            conn.execute(add, ("01HZZZZZZZZZZZZZZZZZZZZZZE", LIST, 0, WORK))
        conn.execute("DELETE FROM work WHERE id = ?", (WORK,))
        assert conn.execute("SELECT count(*) FROM collection_item").fetchone()[0] == 0
        conn.execute("DELETE FROM collection WHERE id = ?", (LIST,))
