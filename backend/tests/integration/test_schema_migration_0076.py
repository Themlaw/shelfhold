"""Migration 0076 (M4 L46): the device token (ADR-0053 §4, ADR-0049).

The columns M3 laid on `api_key` (`subject_user_id`, `issued_by`) do not suffice: an API key is an
`shk_…` bearer secret with scopes, listed, rotated and refused by shape, while a device token has
a public identifier, one stored digest and a closed reach. Two tables, so that neither can ever
be taken for the other. The DDL parity of the whole database is
`test_migrated_ddl_equals_create_all`.
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

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-USR-008")

USER = new_ulid()
OTHER = new_ulid()


@pytest.mark.req("ENF-OPS-002")
def test_single_alembic_head_with_0076_on_0071() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1
    assert script.get_revision("0076").down_revision == "0071"


@pytest.mark.req("ENF-OPS-002")
def test_upgrade_from_0071_keeps_data_and_adds_the_table(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0071")
    assert "device_token" not in tables(database)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO provider_quota (provider, tokens, refilled_at) "
            "VALUES ('openlibrary', 1.0, '2026-11-01 10:00:00')"
        )
    assert "0076" in migrate(settings).applied
    assert "device_token" in tables(database)
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT count(*) FROM provider_quota").fetchone() == (1,)


def _user(conn: sqlite3.Connection, user_id: str, name: str) -> None:
    conn.execute(
        'INSERT INTO "user" (id, username, role, created_at) '
        "VALUES (?, ?, 'member', '2026-11-01 10:00:00')",
        (user_id, name),
    )


def _token(conn: sqlite3.Connection, **override: object) -> None:
    row: dict[str, object] = {
        "id": new_ulid(),
        "public_id": "abcd2345",
        "subject_user_id": USER,
        "secret_digest": "sha256:" + "0" * 64,
        "created_at": "2026-11-01 10:00:00",
    }
    row.update(override)
    conn.execute(
        f"INSERT INTO device_token ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608
        tuple(row.values()),
    )


def test_device_token_rejects_a_digest_that_is_not_prefixed_and_a_duplicate_identifier(
    settings: Settings,
) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _user(conn, USER, "reader")
        _token(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _token(conn, id=new_ulid(), secret_digest="0" * 64)  # not prefixed
        with pytest.raises(sqlite3.IntegrityError):
            _token(conn, id=new_ulid())  # same public identifier
        with pytest.raises(sqlite3.IntegrityError):  # no subject, no token (no default profile)
            _token(conn, id=new_ulid(), public_id="zzzz2345", subject_user_id=None)


def test_deleting_the_subject_deletes_the_token_and_deleting_the_issuer_keeps_it(
    settings: Settings,
) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _user(conn, USER, "reader")
        _user(conn, OTHER, "parent")
        _token(conn, issued_by=OTHER)
        conn.execute('DELETE FROM "user" WHERE id = ?', (OTHER,))
        assert conn.execute("SELECT issued_by FROM device_token").fetchall() == [(None,)]
        conn.execute('DELETE FROM "user" WHERE id = ?', (USER,))
        assert conn.execute("SELECT count(*) FROM device_token").fetchone() == (0,)


@pytest.mark.req("ENF-OPS-002")
def test_downgrade_to_0071_drops_the_table(settings: Settings) -> None:
    migrate(settings)
    command.downgrade(alembic_config(settings.database_path), "0071")
    assert "device_token" not in tables(settings.database_path)
