"""Migration 0094 (M4 L39, EF-INT-021, ENF-SEC-005): `notify_destination`.

One row per adult profile that has a destination of its own, the URL in the sealed envelope
columns. Nothing existing is touched; reversible.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-INT-021", "ENF-SEC-005")

USER = "01HZZZZZZZZZZZZZZZZZZZZZZB"
ROW = "01HZZZZZZZZZZZZZZZZZZZZZZA"


def test_the_revision_is_0094_the_only_head_on_0093() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_revision("0094").down_revision == "0093"


def test_the_table_comes_and_goes_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0093")
    assert "notify_destination" not in tables(database)
    assert "0094" in migrate(settings).applied
    assert "notify_destination" in tables(database)
    command.downgrade(alembic_config(database), "0093")
    assert "notify_destination" not in tables(database)


def test_a_row_holds_a_secret_and_goes_with_its_profile(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute(
            "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'a', 'member', 0)", (USER,)
        )
        with pytest.raises(sqlite3.IntegrityError):  # a row with no secret is no destination
            conn.execute("INSERT INTO notify_destination (id, user_id) VALUES (?, ?)", (ROW, USER))
        conn.execute("INSERT INTO secret_key (kid, state) VALUES ('k1', 'active')")
        conn.execute(
            "INSERT INTO notify_destination (id, user_id, secret_kid, secret_v, secret_nonce, "
            "secret_ct) VALUES (?, ?, 'k1', 1, x'00', zeroblob(16))",
            (ROW, USER),
        )
        with pytest.raises(sqlite3.IntegrityError):  # one destination per profile
            conn.execute(
                "INSERT INTO notify_destination (id, user_id, secret_kid, secret_v, secret_nonce, "
                "secret_ct) VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZC', ?, 'k1', 1, x'00', zeroblob(16))",
                (USER,),
            )
        conn.execute("DELETE FROM user WHERE id = ?", (USER,))
        assert conn.execute("SELECT count(*) FROM notify_destination").fetchone() == (0,)
