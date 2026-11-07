"""M1b schema, migration 0013 (PLAN-M1b L01): passkeys, backup runs, connected-service identity."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import current_revision, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

USER = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
CRED = "01ARZ3NDEKTSV4RRFFQ69G5FAW"
OTHER = "01ARZ3NDEKTSV4RRFFQ69G5FAX"


def insert_user(db: sqlite3.Connection) -> None:
    db.execute(
        "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'admin', 'admin', 1)", (USER,)
    )


def insert_credential(db: sqlite3.Connection, row_id: str, credential_id: bytes) -> None:
    db.execute(
        "INSERT INTO webauthn_credential (id, user_id, credential_id, public_key, name) "
        "VALUES (?, ?, ?, x'01', 'phone')",
        (row_id, USER, credential_id),
    )


@pytest.mark.req("ENF-OPS-002", "EF-USR-025", "EF-INT-015", "EF-SRC-036")
def test_upgrade_from_0012_keeps_data(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0012")
    with closing(sqlite3.connect(database)) as db:
        insert_user(db)
        db.execute("INSERT INTO source_config (plugin_id, settings) VALUES ('suwayomi', '{}')")
        db.commit()
    assert current_revision(database) == "0012"

    result = migrate(settings)

    assert result.applied[0] == "0013"
    assert {"webauthn_credential", "backup_run"} <= tables(database)
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT username FROM user").fetchall() == [("admin",)]
        assert db.execute(
            "SELECT plugin_id, instance_fingerprint, instance_fingerprint_seen_at "
            "FROM source_config"
        ).fetchall() == [("suwayomi", None, None)]


@pytest.mark.req("EF-USR-025")
def test_webauthn_credential_id_unique(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        insert_user(db)
        insert_credential(db, CRED, b"abc")
        with pytest.raises(sqlite3.IntegrityError, match="credential_id"):
            insert_credential(db, OTHER, b"abc")
        insert_credential(db, OTHER, b"xyz")
        row = db.execute(
            "SELECT sign_count, transports, revoked_at FROM webauthn_credential WHERE id = ?",
            (CRED,),
        ).fetchone()
        assert row == (0, "[]", None)  # fresh credential: counter at zero, not revoked
        db.execute("DELETE FROM user")  # a credential goes with its user
        assert db.execute("SELECT count(*) FROM webauthn_credential").fetchone() == (0,)


@pytest.mark.req("EF-INT-015")
def test_backup_run_states_and_checks(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        insert = (
            "INSERT INTO backup_run (id, started_at, state, size, sha256) "
            "VALUES (?, '2026-11-07 03:00:00.000000', ?, ?, ?)"
        )
        db.execute(insert, (CRED, "running", None, None))
        assert db.execute("SELECT state FROM backup_run").fetchone() == ("running",)
        for bad in (
            (OTHER, "done", None, None),  # not a state
            (OTHER, "ok", -1, None),  # negative size
            (OTHER, "ok", 1, "xyz"),  # not a sha256
        ):
            with pytest.raises(sqlite3.IntegrityError):
                db.execute(insert, bad)
        db.execute(insert, (OTHER, "ok", 10, "a" * 64))


@pytest.mark.req("EF-SRC-036")
def test_source_config_carries_the_instance_fingerprint(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path)) as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(source_config)")}
        assert {"instance_fingerprint", "instance_fingerprint_seen_at"} <= columns
        columns = {row[1] for row in db.execute("PRAGMA table_info(source_failure)")}
        assert "consecutive_content_errors" in columns  # the counter already exists (EF-BIB-042)
