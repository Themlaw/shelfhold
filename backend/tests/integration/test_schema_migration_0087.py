"""Migration 0087 (M4 L30, EF-USR-019): a request put aside, and the proposals of a search.

`request.state` takes `to_buy` and `borrow_elsewhere`, `request.elsewhere_url` keeps an `https`
address, `request_proposal` holds at most five results per request. The table is rebuilt (SQLite
cannot change a `CHECK`): what it held stays, reversible. The head of the migrations is pinned
here, by the newest one."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-USR-019")

USER = "01HZZZZZZZZZZZZZZZZZZZZZZA"
REQUEST = "01HZZZZZZZZZZZZZZZZZZZZZZB"
ITEM = "01HZZZZZZZZZZZZZZZZZZZZZZC"


def test_the_revision_is_0087_the_only_head_on_0086() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0087"]
    assert script.get_revision("0087").down_revision == "0086"


def _seed(conn: sqlite3.Connection, state: str = "pending") -> None:
    conn.execute("INSERT INTO user (id, username, role) VALUES (?, 'someone', 'member')", (USER,))
    conn.execute(
        "INSERT INTO request (id, requester_user_id, target_kind, target_value, state) "
        "VALUES (?, ?, 'free_text', 'x', ?)", (REQUEST, USER, state),
    )  # fmt: skip


def test_the_table_is_rebuilt_without_losing_a_row_and_goes_back(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0086")
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        _seed(conn, "arrived")
    assert "0087" in migrate(settings).applied
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        assert conn.execute("SELECT state, elsewhere_url FROM request").fetchall() == [
            ("arrived", None)
        ]
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"ix_request_requester", "ix_request_work"} <= names
        conn.execute("UPDATE request SET state = 'to_buy'")
    assert "request_proposal" in tables(database)
    command.downgrade(alembic_config(database), "0086")
    assert "request_proposal" not in tables(database)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        assert conn.execute("SELECT state FROM request").fetchall() == [("pending",)]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE request SET state = 'to_buy'")


def test_only_two_new_states_and_an_https_address(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _seed(conn)
        for state in ("to_buy", "borrow_elsewhere"):
            conn.execute("UPDATE request SET state = ?", (state,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE request SET state = 'lent'")
        for url in ("http://shop.example/x", "javascript:alert(1)", "https://x/" + "a" * 2100):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute("UPDATE request SET elsewhere_url = ?", (url,))
        conn.execute("UPDATE request SET elsewhere_url = 'https://shop.example/x'")


def test_proposals_are_at_most_five_and_go_with_their_request(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _seed(conn)
        conn.execute(
            "INSERT INTO pending_source_item (id, source_key, series_ref, title, fetched_at) "
            "VALUES (?, 'alpha', 'r1', 'T', '2026-11-16 10:00:00')", (ITEM,),
        )  # fmt: skip
        for position in (0, 6):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO request_proposal (id, request_id, source_item_id, position) "
                    "VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZD', ?, ?, ?)", (REQUEST, ITEM, position),
                )  # fmt: skip
        conn.execute(
            "INSERT INTO request_proposal (id, request_id, source_item_id, position) "
            "VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZD', ?, ?, 1)", (REQUEST, ITEM),
        )  # fmt: skip
        with pytest.raises(sqlite3.IntegrityError):  # the same result twice for one request
            conn.execute(
                "INSERT INTO request_proposal (id, request_id, source_item_id, position) "
                "VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZE', ?, ?, 2)", (REQUEST, ITEM),
            )  # fmt: skip
        conn.execute("DELETE FROM request WHERE id = ?", (REQUEST,))
        assert conn.execute("SELECT count(*) FROM request_proposal").fetchone()[0] == 0
