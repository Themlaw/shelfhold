"""Migration 0101 (M5 L03, EF-USR-004): the OIDC identity table.

`oidc_identity` links an account that already exists to a `(issuer, sub)` pair. The pair is unique
(one person of a provider is one account), an account holds one identity per provider, and an
identity goes with its account. Nothing else of the schema changes. Reversible.
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

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-USR-004")

HEAD_BEFORE = "0100"
USER, OTHER = new_ulid(), new_ulid()
ISSUER = "https://idp.example.test"


def test_the_revision_is_0101_the_head_on_0100() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_revision("0101").down_revision == HEAD_BEFORE


def _users(conn: sqlite3.Connection) -> None:
    for ident, name in ((USER, "mum"), (OTHER, "dad")):
        conn.execute(
            'INSERT INTO "user" (id, username, role) VALUES (?, ?, "member")', (ident, name)
        )


def test_upgrade_adds_the_table_and_changes_nothing_else(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), HEAD_BEFORE)
    before = tables(database)
    assert "oidc_identity" not in before
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        _users(conn)
    command.upgrade(alembic_config(database), "0101")  # this one only: later ones add their own
    assert tables(database) - before == {"oidc_identity"}
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute('SELECT count(*) FROM "user"').fetchone() == (2,)


def test_the_pair_is_unique_and_an_identity_goes_with_its_account(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _users(conn)
        insert = "INSERT INTO oidc_identity (id, user_id, issuer, sub) VALUES (?, ?, ?, ?)"
        conn.execute(insert, (new_ulid(), USER, ISSUER, "sub-1"))
        with pytest.raises(sqlite3.IntegrityError):  # the same person on another account
            conn.execute(insert, (new_ulid(), OTHER, ISSUER, "sub-1"))
        with pytest.raises(sqlite3.IntegrityError):  # an account holds one identity per provider
            conn.execute(insert, (new_ulid(), USER, ISSUER, "sub-2"))
        conn.execute(
            insert, (new_ulid(), USER, "https://other.example.test", "sub-1")
        )  # another one
        with pytest.raises(sqlite3.IntegrityError):  # an empty sub is no identity
            conn.execute(insert, (new_ulid(), OTHER, ISSUER, ""))
        with pytest.raises(sqlite3.IntegrityError):  # no account, no identity
            conn.execute(insert, (new_ulid(), new_ulid(), ISSUER, "sub-9"))
        assert conn.execute(
            "SELECT linked_at IS NOT NULL FROM oidc_identity WHERE sub = 'sub-1' LIMIT 1"
        ).fetchone() == (1,)
        conn.execute('DELETE FROM "user" WHERE id = ?', (USER,))
        assert conn.execute("SELECT count(*) FROM oidc_identity").fetchone() == (0,)


def test_downgrade_to_0100_drops_the_table(settings: Settings) -> None:
    migrate(settings)
    command.downgrade(alembic_config(settings.database_path), HEAD_BEFORE)
    assert "oidc_identity" not in tables(settings.database_path)
    assert "device" in tables(settings.database_path)
