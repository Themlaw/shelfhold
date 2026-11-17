"""Migration 0091 (M4 L29, EF-INT-019, EF-INT-020): `activity_block` and `traffic_counter`.

The blocklist (one row per chapter a source must not bring back, by `(source_key,
remote_unit_id)`) and the traffic counter (one row per source and per day: a count and a size,
never an address). Nothing existing is touched; reversible. The head of the migrations is pinned
here, by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-INT-019", "EF-INT-020")

NEW = {"activity_block", "traffic_counter"}
BLOCK = "01HZZZZZZZZZZZZZZZZZZZZZZA"
USER = "01HZZZZZZZZZZZZZZZZZZZZZZB"


def test_the_revision_is_0091_the_only_head_on_0089() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0091"]
    assert script.get_revision("0091").down_revision == "0089"


def test_the_tables_come_and_go_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0089")
    assert not NEW & tables(database)
    assert "0091" in migrate(settings).applied
    assert tables(database) >= NEW
    command.downgrade(alembic_config(database), "0089")
    assert not NEW & tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def test_a_chapter_is_blocked_once_for_a_known_reason(settings: Settings) -> None:
    add = "INSERT INTO activity_block (id, source_key, remote_unit_id, reason) VALUES (?, ?, ?, ?)"
    with closing(_connect(settings)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (BLOCK, "s", "u", "free text"))  # a closed list of reasons
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (BLOCK, "", "u", "other"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (BLOCK, "s", "", "other"))
        conn.execute(add, (BLOCK, "s", "u", "other"))
        with pytest.raises(sqlite3.IntegrityError):  # one block per chapter of a source
            conn.execute(add, ("01HZZZZZZZZZZZZZZZZZZZZZZC", "s", "u", "duplicate"))
        conn.execute(add, ("01HZZZZZZZZZZZZZZZZZZZZZZC", "other", "u", "duplicate"))  # elsewhere
        assert conn.execute("SELECT proposed_count FROM activity_block").fetchone()[0] == 0


def test_a_block_outlives_the_account_that_made_it(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        conn.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'a', 'admin', 1)",
            (USER,),
        )
        conn.execute(
            "INSERT INTO activity_block (id, source_key, remote_unit_id, reason, created_by) "
            "VALUES (?, 's', 'u', 'other', ?)",
            (BLOCK, USER),
        )
        conn.execute("DELETE FROM user WHERE id = ?", (USER,))
        row = conn.execute("SELECT created_by FROM activity_block").fetchone()
        assert row == (None,)  # the chapter stays blocked: nothing unblocks by itself


def test_the_counter_holds_a_source_a_day_and_two_numbers(settings: Settings) -> None:
    add = "INSERT INTO traffic_counter (day, source_key, requests, bytes_in) VALUES (?, ?, ?, ?)"
    with closing(_connect(settings)) as conn:
        day = "2026-11-16"
        for bad in ((day, "", 1, 1), (day, "s", -1, 0), (day, "s", 0, -1)):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(add, bad)
        conn.execute(add, ("2026-11-16", "s", 3, 300))
        with pytest.raises(sqlite3.IntegrityError):  # one row per source and day
            conn.execute(add, ("2026-11-16", "s", 1, 1))
        conn.execute(add, ("2026-11-17", "s", 1, 1))
        columns = {r[1] for r in conn.execute("PRAGMA table_info(traffic_counter)")}
        assert columns == {"day", "source_key", "requests", "bytes_in"}
