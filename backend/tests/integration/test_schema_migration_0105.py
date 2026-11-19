"""Migration 0105 (M5 L21, EF-BIB-017): `saved_selection`.

A stored query (a closed JSON structure of bounded size) that belongs to one profile and goes with
that profile. It holds no Work and no result: it is evaluated each time it is read. Nothing existing
is touched; reversible.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-017")

USER = "01HZZZZZZZZZZZZZZZZZZZZZZA"
OTHER = "01HZZZZZZZZZZZZZZZZZZZZZZB"
FIRST, SECOND = "01HZZZZZZZZZZZZZZZZZZZZZZ1", "01HZZZZZZZZZZZZZZZZZZZZZZ2"
HEAD_BEFORE = "0107"


def test_the_revision_is_0105_the_head_on_0107() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # one head, whichever later lot moved it
    assert script.get_revision("0105").down_revision == HEAD_BEFORE


def test_the_table_comes_and_goes_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), HEAD_BEFORE)
    assert "saved_selection" not in tables(database)
    assert "0105" in migrate(settings).applied
    assert "saved_selection" in tables(database)
    command.downgrade(alembic_config(database), HEAD_BEFORE)
    assert "saved_selection" not in tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    for ident, name in ((USER, "ann"), (OTHER, "bob")):
        conn.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, ?, 'member', 0)",
            (ident, name),
        )
    return conn


def _selection(
    conn: sqlite3.Connection, ident: str, *, owner: str = USER, name: str = "Unread",
    query: str = '{"kind":["manga"]}',
) -> None:  # fmt: skip
    conn.execute(
        "INSERT INTO saved_selection (id, owner_user_id, name, query_json) VALUES (?, ?, ?, ?)",
        (ident, owner, name, query),
    )


def test_a_selection_goes_with_its_profile_and_only_its_profile(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        _selection(conn, FIRST)
        _selection(conn, SECOND, owner=OTHER)
        conn.execute("DELETE FROM user WHERE id = ?", (USER,))
        assert conn.execute("SELECT id FROM saved_selection").fetchall() == [(SECOND,)]


def test_the_name_and_the_query_are_bounded_and_shaped(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        bad = (
            {"name": ""},
            {"name": "x" * 201},
            {"query": "not json"},
            {"query": "[]"},  # an object, always
            {"query": '{"kind":["' + "a" * 2100 + '"]}'},  # bounded in size
            {"owner": "not-an-id"},
        )
        for over in bad:
            with pytest.raises(sqlite3.IntegrityError):
                _selection(conn, FIRST, **over)  # type: ignore[arg-type]
        with pytest.raises(sqlite3.IntegrityError):
            _selection(conn, "not-an-id")
        _selection(conn, FIRST, name="x" * 200)
        with pytest.raises(sqlite3.IntegrityError):  # an owner is a profile that exists
            _selection(conn, SECOND, owner="01HZZZZZZZZZZZZZZZZZZZZZZZ")
