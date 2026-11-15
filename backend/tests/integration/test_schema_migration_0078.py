"""Migration 0078 (M4 L14b): what the progress synchronisation noted of each chapter.

One table, `progress_sync_note`, per account and Kavita chapter: the page Shelfhold last pushed
(so that the echo of its own push is never read back), the state last seen, and when the chapter
was last synchronised. It goes with the account: removing the profile, the binding or the
account's row removes it.
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
from tests.integration.test_schema_migration_0046 import add_row

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-EXP-026")

USER, DEST, TARGET, SHELF, ACCOUNT = (new_ulid() for _ in range(5))


@pytest.mark.req("ENF-OPS-002")
def test_single_alembic_head_with_0078_on_0077() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # the head itself is pinned by the newest migration test
    assert script.get_revision("0078").down_revision == "0077"


@pytest.mark.req("ENF-OPS-002")
def test_upgrade_adds_the_table_and_downgrade_drops_it(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), "0077")
    assert "progress_sync_note" not in tables(settings.database_path)
    assert "0078" in migrate(settings).applied
    assert "progress_sync_note" in tables(settings.database_path)
    command.downgrade(alembic_config(settings.database_path), "0077")
    assert "progress_sync_note" not in tables(settings.database_path)


def _seed(db: sqlite3.Connection) -> None:
    add_row(db, "user", id=USER, username="reader", role="member", can_rate=0)
    add_row(db, "export_target", id=TARGET, kind="kavita", name="k")
    add_row(db, "shelf", id=SHELF, name="s", phrase="Manga")
    add_row(
        db, "shelf_destination", id=DEST, shelf_id=SHELF, target_id=TARGET, transport="tree",
        egress="foyer", root_subpath="m", template="{series}", template_origin="default@1",
    )  # fmt: skip
    sealed: dict[str, object] = {
        "secret_v": 1, "secret_kid": "k1", "secret_nonce": b"n" * 12, "secret_ct": b"c" * 32,
        "token_fingerprint": "abcd1234",
    }  # fmt: skip
    add_row(
        db,
        "destination_account",
        id=ACCOUNT,
        user_id=USER,
        shelf_destination_id=DEST,
        remote_user="acct",
        **sealed,
    )


def _note(db: sqlite3.Connection, **override: object) -> None:
    row: dict[str, object] = {
        "shelf_destination_id": DEST,
        "user_id": USER,
        "kavita_chapter_id": 101,
        "account_id": ACCOUNT,
        "account_ref": "reader|abcd1234",
        "carrier_id": new_ulid(),
        "synced_at": "2026-11-15 12:00:00",
    }
    row.update(override)
    db.execute(
        f"INSERT INTO progress_sync_note ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608
        tuple(row.values()),
    )


def test_a_note_is_unique_per_account_and_chapter_and_goes_with_the_account(
    settings: Settings,
) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as db:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("INSERT INTO secret_key (kid, state) VALUES ('k1', 'active')")
        _seed(db)
        _note(db)
        with pytest.raises(sqlite3.IntegrityError):
            _note(db)  # the same chapter twice
        with pytest.raises(sqlite3.IntegrityError):
            _note(db, kavita_chapter_id=102, user_id=new_ulid())  # no such account
        with pytest.raises(sqlite3.IntegrityError):
            _note(db, kavita_chapter_id=103, pushed_page=-1)  # a page is not negative
        db.execute("DELETE FROM destination_account WHERE id = ?", (ACCOUNT,))
        assert db.execute("SELECT count(*) FROM progress_sync_note").fetchone() == (0,)
