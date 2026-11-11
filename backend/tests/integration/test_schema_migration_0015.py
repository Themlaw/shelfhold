"""Migration 0015 (review PR #95): the RP ID of a passkey, the passkey behind a session."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

USER = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
CRED = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
SESSION = "01ARZ3NDEKTSV4RRFFQ69G5FAX"
DIGEST = "sha256:" + "0" * 64


def seed_0014(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0014")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'a', 'admin', 1)", (USER,)
        )
        db.execute(
            "INSERT INTO webauthn_credential (id, user_id, credential_id, public_key, name) "
            "VALUES (?, ?, x'0102', x'01', 'phone')",
            (CRED, USER),
        )
        db.execute(
            "INSERT INTO session (id, user_id, token_hash, csrf_secret, created_at, last_seen_at, "
            "auth_time, absolute_expiry, remember) "
            "VALUES (?, ?, ?, 'c', '2026-11-05', '2026-11-05', '2026-11-05', '2026-12-05', 1)",
            (SESSION, USER, DIGEST),
        )
        db.commit()
    assert current_revision(database) == "0014"


@pytest.mark.req("ENF-OPS-002", "EF-USR-025")
def test_upgrade_from_0014_keeps_data(settings: Settings) -> None:
    seed_0014(settings)
    url = "https://Shelf.Example.org:8443/path"
    result = migrate(settings.model_copy(update={"public_url": url}))
    assert result.applied == ("0015", "0016", "0017", "0020", "0022")
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute("SELECT name, rp_id FROM webauthn_credential").fetchall() == [
            ("phone", "shelf.example.org")  # what the rows were registered under
        ]
        assert db.execute("SELECT id, passkey_id FROM session").fetchall() == [(SESSION, None)]
        db.execute("UPDATE session SET passkey_id = ?", (CRED,))
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.req("ENF-OPS-002", "EF-USR-025")
def test_upgrade_without_a_public_url_leaves_the_rp_id_unknown(settings: Settings) -> None:
    seed_0014(settings)
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute("SELECT rp_id FROM webauthn_credential").fetchall() == [(None,)]
