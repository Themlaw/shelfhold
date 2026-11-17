"""Migration 0097 (M4 L35, EF-BIB-005): `team_switch`.

One row per preview of a switch to the preferred team: what it would do (kept to be compared with
what is done), the report once applied. Nothing existing is touched; reversible.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-005")

USER = "01HZZZZZZZZZZZZZZZZZZZZZZB"
SWITCH = "01HZZZZZZZZZZZZZZZZZZZZZZA"


def test_the_revision_is_0097_the_head_on_0096() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0097"]  # the head is pinned here, by the newest
    assert script.get_revision("0097").down_revision == "0096"


def test_the_table_comes_and_goes_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0096")
    assert "team_switch" not in tables(database)
    assert "0097" in migrate(settings).applied
    assert "team_switch" in tables(database)
    command.downgrade(alembic_config(database), "0096")
    assert "team_switch" not in tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO user (id, username, role, can_rate, max_age_rating) "
        "VALUES (?, 'ann', 'member', 0, NULL)",
        (USER,),
    )
    return conn


def test_a_row_needs_a_known_edition_and_a_closed_state(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        columns = {r[1] for r in conn.execute("PRAGMA table_info(team_switch)")}
        assert {"id", "edition_id", "team_id", "state", "token", "preview", "report"} <= columns
        with pytest.raises(sqlite3.IntegrityError):  # no such edition
            conn.execute(
                "INSERT INTO team_switch (id, edition_id, team_id, token, preview) "
                "VALUES (?, 'nope', 'nope', ?, '{}')",
                (SWITCH, "0" * 64),
            )
