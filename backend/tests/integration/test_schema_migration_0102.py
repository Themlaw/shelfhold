"""Migration 0102 (M5 L07, EF-LEC-007): `reading_conflict`.

The divergence of two devices on one carrier, proposed once to the reader until she chooses:
identifiers and a choice only (no locator copy, the positions are the history). At most one open
proposal per profile and carrier. It goes with its user. Nothing existing is touched; reversible.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-LEC-007")

USER = "01HZZZZZZZZZZZZZZZZZZZZZZB"
CARRIER = "01HZZZZZZZZZZZZZZZZZZZZZZC"
WIN, LOSE = "01HZZZZZZZZZZZZZZZZZZZZZZD", "01HZZZZZZZZZZZZZZZZZZZZZZE"


def test_the_revision_is_0102_the_head_on_0101() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0102"]  # the head is pinned here, by the newest
    assert script.get_revision("0102").down_revision == "0101"


def test_the_table_comes_and_goes_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0101")
    assert "reading_conflict" not in tables(database)
    assert "0102" in migrate(settings).applied
    assert "reading_conflict" in tables(database)
    command.downgrade(alembic_config(database), "0101")
    assert "reading_conflict" not in tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'ann', 'admin', 1)", (USER,)
    )
    return conn


def _conflict(conn: sqlite3.Connection, ident: str, **over: str | None) -> None:
    values: dict[str, str | None] = {
        "carrier_type": "content",
        "choice": None,
        "resolved_at": None,
    } | over
    conn.execute(
        "INSERT INTO reading_conflict (id, user_id, carrier_type, carrier_id, winner_position_id,"
        " loser_position_id, choice, resolved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (ident, USER, values["carrier_type"], CARRIER, WIN, LOSE, values["choice"],
         values["resolved_at"]),
    )  # fmt: skip


def test_one_open_proposal_per_profile_and_carrier(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        _conflict(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ1")
        with pytest.raises(sqlite3.IntegrityError):
            _conflict(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ2")  # proposed once
        conn.execute(
            "UPDATE reading_conflict SET choice = 'winner', resolved_at = CURRENT_TIMESTAMP"
        )
        _conflict(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ2")  # settled: a new divergence may be proposed
        assert conn.execute("SELECT count(*) FROM reading_conflict").fetchone() == (2,)


def test_the_choice_and_the_carrier_are_closed(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        for bad in ({"carrier_type": "unit"}, {"choice": "both"}, {"choice": "winner"}):
            with pytest.raises(sqlite3.IntegrityError):
                _conflict(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ1", **bad)  # a choice needs its date
        with pytest.raises(sqlite3.IntegrityError):
            _conflict(conn, "not-an-id")
        _conflict(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ1", choice="loser", resolved_at="2026-11-18")


def test_a_proposal_goes_with_its_user(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        _conflict(conn, "01HZZZZZZZZZZZZZZZZZZZZZZ1")
        conn.execute("DELETE FROM user WHERE id = ?", (USER,))
        assert conn.execute("SELECT count(*) FROM reading_conflict").fetchone() == (0,)
