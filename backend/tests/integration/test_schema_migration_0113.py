"""Migration 0113 (M6 L13, EF-INT-010): `tracker_link` and `tracker_allowance`.

One link per profile and per tracker plugin: the remote reference the plugin returned, whether
the profile said yes to the push (never by default), and the remote account's token sealed to
the row (ENF-SEC-005) with the fingerprint the API shows. The allowance is the admin's word that
one tracker may be linked by one child profile (EF-USR-024): a row means allowed, no row means
refused. Reversible: the downgrade drops both tables.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-INT-010")

HEAD_BEFORE = "0114"
USER = "01J00000000000000000000USR"
OTHER = "01J00000000000000000000OTH"


def test_the_revision_is_0113_the_head_on_0114() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # one head, whichever later lot moved it
    assert script.get_revision("0113").down_revision == HEAD_BEFORE


def _user(conn: sqlite3.Connection, ident: str, role: str = "member") -> None:
    conn.execute(
        f'INSERT INTO "user" (id, username, role) VALUES (?, ?, ?)', (ident, ident[-4:], role)
    )


def test_the_tables_arrive_with_their_constraints(settings: Settings) -> None:
    assert "0113" in migrate(settings).applied
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        _user(conn, USER)
        _user(conn, OTHER)
        conn.execute(
            "INSERT INTO tracker_link (id, user_id, plugin_key, remote_ref, linked_at,"
            " push_enabled, secret_ct, secret_nonce, token_fingerprint)"
            " VALUES ('01J00000000000000000000LNK', ?, 'fake-tracker', '{}',"
            " '2026-11-20 12:00:00', 0, x'00', x'00', 'abc')",
            (USER,),
        )
        # the push is never on by default
        conn.execute(
            "INSERT INTO tracker_link (id, user_id, plugin_key, remote_ref, linked_at)"
            " VALUES ('01J00000000000000000000LN2', ?, 'other-tracker', '{}',"
            " '2026-11-20 12:00:00')",
            (USER,),
        )
        assert conn.execute("SELECT push_enabled FROM tracker_link").fetchall() == [(0,), (0,)]
        # one link per profile and per tracker
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracker_link (id, user_id, plugin_key, remote_ref, linked_at)"
                " VALUES ('01J00000000000000000000LN3', ?, 'fake-tracker', '{}',"
                " '2026-11-20 12:00:00')",
                (USER,),
            )
        # a link belongs to a profile that exists
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracker_link (id, user_id, plugin_key, remote_ref, linked_at)"
                " VALUES ('01J00000000000000000000LN4', '01JZZZZZZZZZZZZZZZZZZZZZZZ',"
                " 'fake-tracker', '{}', '2026-11-20 12:00:00')"
            )
        # the allowance: one row per profile and per tracker, naming who allowed it
        conn.execute(
            "INSERT INTO tracker_allowance (user_id, plugin_key, allowed_by, allowed_at)"
            " VALUES (?, 'fake-tracker', ?, '2026-11-20 12:00:00')",
            (OTHER, USER),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tracker_allowance (user_id, plugin_key, allowed_by, allowed_at)"
                " VALUES (?, 'fake-tracker', ?, '2026-11-20 12:00:01')",
                (OTHER, USER),
            )


def test_downgrade_to_0114_drops_the_tables(settings: Settings) -> None:
    migrate(settings)
    command.downgrade(alembic_config(settings.database_path), HEAD_BEFORE)
    with closing(sqlite3.connect(settings.database_path)) as conn:
        names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    assert "tracker_link" not in names
    assert "tracker_allowance" not in names
    assert "reader_setting" in names  # the rest of the schema stays
