"""Migration 0100 (M5 L02, EF-USR-008): the device registry.

`device` is what a pairing makes: a reader profile, a label, a kind from a closed list, when it was
seen last. Each existing device token receives a device of its own (kind `other`, its name as
label, its last use as last seen, revoked as it is), so that nothing paired before the migration is
lost or left outside the registry. Reversible.
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
HEAD_BEFORE = "0098"


def test_the_revision_is_0100_on_0098() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_revision("0100") is not None  # the head is pinned by the newest, 0101
    assert script.get_revision("0100").down_revision == HEAD_BEFORE


def _seed(conn: sqlite3.Connection) -> None:
    conn.execute(
        'INSERT INTO "user" (id, username, role, created_at) '
        "VALUES (?, 'reader', 'member', '2026-11-01 10:00:00')",
        (USER,),
    )
    for public_id, name, used, revoked in (
        ("abcd2345", "Salon", "2026-11-10 08:00:00", None),
        ("efgh6789", None, None, "2026-11-12 09:00:00"),
    ):
        conn.execute(
            "INSERT INTO device_token (id, public_id, name, subject_user_id, secret_digest, "
            "last_used_at, revoked_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, "
            "'2026-11-02 10:00:00')",
            (new_ulid(), public_id, name, USER, "sha256:" + "0" * 64, used, revoked),
        )


def test_upgrade_gives_every_existing_token_a_device(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), HEAD_BEFORE)
    assert "device" not in tables(database)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        _seed(conn)
    assert "0100" in migrate(settings).applied
    assert "device" in tables(database)
    with closing(sqlite3.connect(database)) as conn:
        rows = conn.execute(
            "SELECT d.subject_user_id, d.label, d.kind, d.last_seen_at, d.revoked_at, "
            "d.created_at, t.public_id FROM device d JOIN device_token t ON t.device_id = d.id "
            "ORDER BY t.public_id"
        ).fetchall()
        assert rows == [
            (USER, "Salon", "other", "2026-11-10 08:00:00", None, "2026-11-02 10:00:00",
             "abcd2345"),
            (USER, None, "other", None, "2026-11-12 09:00:00", "2026-11-02 10:00:00",
             "efgh6789"),
        ]  # fmt: skip
        assert conn.execute("SELECT count(*) FROM device").fetchone() == (2,)
        assert conn.execute(
            "SELECT count(*) FROM device_token WHERE device_id IS NULL"
        ).fetchone() == (0,)


def test_the_kind_is_a_closed_list_and_a_device_goes_with_its_profile(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            'INSERT INTO "user" (id, username, role) VALUES (?, "reader", "member")', (USER,)
        )
        for kind in ("koreader", "kobo", "opds", "browser", "other"):
            conn.execute(
                "INSERT INTO device (id, subject_user_id, kind) VALUES (?, ?, ?)",
                (new_ulid(), USER, kind),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO device (id, subject_user_id, kind) VALUES (?, ?, 'toaster')",
                (new_ulid(), USER),
            )
        with pytest.raises(sqlite3.IntegrityError):  # no profile, no device
            conn.execute(
                "INSERT INTO device (id, subject_user_id, kind) VALUES (?, NULL, 'other')",
                (new_ulid(),),
            )
        conn.execute('DELETE FROM "user" WHERE id = ?', (USER,))
        assert conn.execute("SELECT count(*) FROM device").fetchone() == (0,)


def test_downgrade_to_0098_drops_the_registry_and_keeps_the_tokens(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), HEAD_BEFORE)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        _seed(conn)
    migrate(settings)
    command.downgrade(alembic_config(database), HEAD_BEFORE)
    assert "device" not in tables(database)
    with closing(sqlite3.connect(database)) as conn:
        assert conn.execute("SELECT count(*) FROM device_token").fetchone() == (2,)
        columns = [r[1] for r in conn.execute("PRAGMA table_info(device_token)")]
        assert "device_id" not in columns
