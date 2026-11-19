"""Migration 0107 (M5 L04, EF-USR-024): `session.proof`, what proved a session.

`proof` is `local` or `provider`, NOT NULL. A session that exists when the migration runs is
`provider` when the audit says a sign-in at the provider opened it or when it never had a local
proof (its `auth_time` is still the epoch), `local` otherwise: the safe side, since a session
wrongly `provider` only proves itself locally once more. Reversible.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-USR-024")

HEAD_BEFORE = "0101"
USER = new_ulid()
SINCE = datetime(2026, 1, 1, tzinfo=UTC).isoformat(sep=" ")
EPOCH = datetime(1970, 1, 1, tzinfo=UTC).isoformat(sep=" ")


def test_the_revision_is_0107_the_head_on_0101() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0107"]  # the head is pinned here, by the newest
    assert script.get_revision("0107").down_revision == HEAD_BEFORE


def _session(conn: sqlite3.Connection, ident: str, auth_time: str) -> None:
    conn.execute(
        "INSERT INTO session (id, user_id, token_hash, csrf_secret, created_at, last_seen_at,"
        " auth_time, absolute_expiry, remember) VALUES (?, ?, ?, 'c', ?, ?, ?, ?, 1)",
        (
            ident,
            USER,
            "sha256:" + sha256(ident.encode()).hexdigest(),
            SINCE,
            SINCE,
            auth_time,
            "2099-01-01 00:00:00",
        ),
    )


def test_existing_sessions_get_the_expected_proof(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), HEAD_BEFORE)
    opened, never, typed = new_ulid(), new_ulid(), new_ulid()
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        conn.execute('INSERT INTO "user" (id, username, role) VALUES (?, "mum", "member")', (USER,))
        _session(conn, opened, SINCE)  # the audit names it
        _session(conn, never, EPOCH)  # no local proof ever, the audit is gone
        _session(conn, typed, SINCE)  # a password
        conn.execute(
            "INSERT INTO audit (id, actor_id, action, at, session_id) VALUES (?, ?, ?, ?, ?)",
            (new_ulid(), USER, "auth.oidc_login", SINCE, opened),
        )
    assert "0107" in migrate(settings).applied
    with closing(sqlite3.connect(settings.database_path)) as conn:
        proofs = dict(conn.execute("SELECT id, proof FROM session").fetchall())
    assert proofs == {opened: "provider", never: "provider", typed: "local"}


def test_a_new_session_is_local_by_default_and_the_value_is_checked(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        conn.execute('INSERT INTO "user" (id, username, role) VALUES (?, "mum", "member")', (USER,))
        ident = new_ulid()
        _session(conn, ident, SINCE)
        assert conn.execute("SELECT proof FROM session WHERE id = ?", (ident,)).fetchone() == (
            "local",
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE session SET proof = 'other' WHERE id = ?", (ident,))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE session SET proof = NULL WHERE id = ?", (ident,))


def test_downgrade_to_0101_drops_the_column(settings: Settings) -> None:
    migrate(settings)
    command.downgrade(alembic_config(settings.database_path), HEAD_BEFORE)
    with closing(sqlite3.connect(settings.database_path)) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(session)")}
    assert "proof" not in columns
    assert "passkey_id" in columns
