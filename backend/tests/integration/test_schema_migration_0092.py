"""Migration 0092 (M4 L31, EF-USR-020): `request_list_import` and `request_list_item`.

One batch per import, for one profile, with its reading shelf; one row per line, either in the
library or a request. Nothing existing is touched; reversible. The head of the migrations is pinned
here, by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-USR-020")

NEW = {"request_list_import", "request_list_item"}
USER = "01HZZZZZZZZZZZZZZZZZZZZZZB"
BATCH = "01HZZZZZZZZZZZZZZZZZZZZZZA"
ITEM = "01HZZZZZZZZZZZZZZZZZZZZZZC"
REQUEST = "01HZZZZZZZZZZZZZZZZZZZZZZD"


def test_the_revision_is_0092_the_only_head_on_0091() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0092"]
    assert script.get_revision("0092").down_revision == "0091"


def test_the_tables_come_and_go_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0091")
    assert not NEW & tables(database)
    assert "0092" in migrate(settings).applied
    assert tables(database) >= NEW
    command.downgrade(alembic_config(database), "0091")
    assert not NEW & tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'kid', 'child', 0)", (USER,)
    )
    return conn


def test_a_batch_has_a_name_and_goes_with_its_profile(settings: Settings) -> None:
    add = "INSERT INTO request_list_import (id, user_id, name) VALUES (?, ?, ?)"
    with closing(_connect(settings)) as conn:
        for name in ("", "x" * 201):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(add, (BATCH, USER, name))
        with pytest.raises(sqlite3.IntegrityError):  # a profile that does not exist
            conn.execute(add, (BATCH, "01HZZZZZZZZZZZZZZZZZZZZZZZ", "list"))
        conn.execute(add, (BATCH, USER, "list"))
        conn.execute(
            "INSERT INTO request_list_item (id, import_id, position, line_text, outcome) "
            "VALUES (?, ?, 0, 'a title', 'in_library')",
            (ITEM, BATCH),
        )
        conn.execute("DELETE FROM user WHERE id = ?", (USER,))
        assert conn.execute("SELECT COUNT(*) FROM request_list_import").fetchone() == (0,)
        assert conn.execute("SELECT COUNT(*) FROM request_list_item").fetchone() == (0,)


def test_a_line_is_in_the_library_or_requested_once_per_place(settings: Settings) -> None:
    add = (
        "INSERT INTO request_list_item (id, import_id, position, line_text, outcome) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    with closing(_connect(settings)) as conn:
        conn.execute(
            "INSERT INTO request_list_import (id, user_id, name) VALUES (?, ?, 'list')",
            (BATCH, USER),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (ITEM, BATCH, 0, "t", "found"))  # a closed list of outcomes
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (ITEM, BATCH, -1, "t", "requested"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (ITEM, BATCH, 0, "t" * 301, "requested"))
        conn.execute(add, (ITEM, BATCH, 0, "t", "requested"))
        with pytest.raises(sqlite3.IntegrityError):  # one line per place of a batch
            conn.execute(add, ("01HZZZZZZZZZZZZZZZZZZZZZZE", BATCH, 0, "u", "requested"))
        assert conn.execute("SELECT annotated FROM request_list_item").fetchone() == (0,)


def test_a_request_that_goes_leaves_its_line(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        conn.execute(
            "INSERT INTO request_list_import (id, user_id, name) VALUES (?, ?, 'list')",
            (BATCH, USER),
        )
        conn.execute(
            "INSERT INTO request (id, requester_user_id, target_kind, target_value) "
            "VALUES (?, ?, 'free_text', 'a title')",
            (REQUEST, USER),
        )
        conn.execute(
            "INSERT INTO request_list_item "
            "(id, import_id, position, line_text, outcome, request_id) "
            "VALUES (?, ?, 0, 'a title', 'requested', ?)",
            (ITEM, BATCH, REQUEST),
        )
        conn.execute("DELETE FROM request WHERE id = ?", (REQUEST,))
        row = conn.execute("SELECT request_id, outcome FROM request_list_item").fetchone()
        assert row == (None, "requested")  # the line stays on the screen, with no request behind
