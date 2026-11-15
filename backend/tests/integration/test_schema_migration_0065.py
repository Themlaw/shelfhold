"""Migration 0065 (M4 L14a): the view `reading_progress` is replaced by the merge."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config


@pytest.mark.req("ENF-OPS-002", "EF-LEC-014")
def test_single_alembic_head_with_0065_on_0076() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # later migrations move the head, never fork it
    assert script.get_revision("0065").down_revision == "0076"


@pytest.mark.req("ENF-OPS-002", "EF-LEC-014")
def test_upgrade_replaces_the_view_and_downgrade_restores_the_old_one(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0076")
    with closing(sqlite3.connect(database)) as conn:
        before = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'reading_progress'"
        ).fetchone()[0]
        assert "min(" not in before.lower()
    assert "0065" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as conn:
        after = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'reading_progress'"
        ).fetchone()[0]
        assert "min(" in after.lower()  # `client_ts` bounded by `server_ts`
        assert conn.execute("SELECT * FROM reading_progress").fetchall() == []
    command.downgrade(alembic_config(database), "0076")
    with closing(sqlite3.connect(database)) as conn:
        again = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'reading_progress'"
        ).fetchone()[0]
    assert again == before


@pytest.mark.req("ENF-OPS-002", "EF-LEC-014")
def test_a_page_written_as_text_before_the_migration_is_ignored_after_it(
    settings: Settings,
) -> None:
    """The positions of the 0004 had no check on the page: a text one is dropped from its locator
    (the position stays, its page is ignored), and the view cannot see it any more."""
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0076")
    ulid, sha = "01HZZZZZZZZZZZZZZZZZZZZZZ1", "sha256:" + "a" * 64
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'someone', 'admin', 1)",
            (ulid,),
        )
        for index, page in enumerate(['"7"', "7.5", "7"]):
            conn.execute(
                "INSERT INTO reading_position (id, user_id, carrier_type, carrier_id, "
                "content_hash, locator, client_ts) VALUES "
                "(?, ?, 'export_item', ?, ?, ?, '2026-11-01 10:00:00')",
                (f"01HZZZZZZZZZZZZZZZZZZZZZZ{index + 2}", ulid, ulid, sha, f'{{"page": {page}}}'),
            )
    assert "0065" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as conn:
        pages = [
            r[0]
            for r in conn.execute(
                "SELECT json_extract(locator, '$.page') FROM reading_position ORDER BY id"
            )
        ]
        assert pages == [None, None, 7]
        assert conn.execute(
            "SELECT source, read_units FROM reading_position LIMIT 1"
        ).fetchone() == (
            "device",
            "[]",
        )
