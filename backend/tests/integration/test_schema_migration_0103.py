"""Migration 0103 (M5 L13, EF-LEC-006): `reader_settings`.

The choices of one profile: a direction that may be pinned to a series (or the profile's own
default), a comfort setting that belongs to one device or to every one of them. One row per
place and profile; it goes with its user. Nothing existing is touched; reversible.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-LEC-006")

USER = "01HZZZZZZZZZZZZZZZZZZZZZZB"
SERIES = "01HZZZZZZZZZZZZZZZZZZZZZZC"


def test_the_revision_is_0103_the_head_on_0102() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0103"]  # the head is pinned here, by the newest
    # chained on 0102 (L07), the head of this branch's base; re-chained at the merge
    assert script.get_revision("0103").down_revision == "0102"


def test_the_table_comes_and_goes_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0102")
    assert "reader_settings" not in tables(database)
    assert "0103" in migrate(settings).applied
    assert "reader_settings" in tables(database)
    command.downgrade(alembic_config(database), "0102")
    assert "reader_settings" not in tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'ann', 'admin', 1)", (USER,)
    )
    return conn


def _setting(conn: sqlite3.Connection, ident: str, **over: str | None) -> None:
    values: dict[str, str | None] = {
        "scope": "everywhere",
        "device_id": None,
        "kind": "direction",
        "key": "default",
        "value": "ltr",
    } | over
    conn.execute(
        "INSERT INTO reader_settings (id, user_id, scope, device_id, kind, key, value)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ident, USER, values["scope"], values["device_id"], values["kind"],
         values["key"], values["value"]),
    )  # fmt: skip


def test_one_row_per_place_and_profile(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        _setting(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ1")
        with pytest.raises(sqlite3.IntegrityError):
            _setting(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ2")  # the same place written twice
        _setting(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ3", scope="device", device_id="web-1")
        _setting(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ4", scope="device", device_id="web-2")
        _setting(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ5", key=SERIES, value="rtl")
        assert conn.execute("SELECT count(*) FROM reader_settings").fetchone() == (4,)


def test_the_portee_names_the_device_or_no_one(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        for bad in (
            {"scope": "device"},  # « pour cet appareil » without saying which one
            {"scope": "everywhere", "device_id": "web-1"},  # « partout » names no device
            {"scope": "somewhere"},
            {"kind": "theme"},
            {"key": ""},
            {"value": ""},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _setting(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ1", **bad)  # type: ignore[arg-type]


def test_the_rows_go_with_their_user(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        _setting(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ1")
        conn.execute("DELETE FROM user WHERE id = ?", (USER,))
        assert conn.execute("SELECT count(*) FROM reader_settings").fetchone() == (0,)
