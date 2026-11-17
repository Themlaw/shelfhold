"""Migration 0090 (M4 L27, EF-BIB-014): `source_migration` and `source_migration_row`.

The table of a series' migration to another source and its lines. They go with their Edition and
their table; a line never lands twice on one place; nothing else is touched; reversible. The head
of the migrations is pinned here, by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-014")

NEW = {"source_migration", "source_migration_row"}
WORK = "01HZZZZZZZZZZZZZZZZZZZZZZA"
EDITION = "01HZZZZZZZZZZZZZZZZZZZZZZB"
TABLE = "01HZZZZZZZZZZZZZZZZZZZZZZC"
SECOND = "01HZZZZZZZZZZZZZZZZZZZZZZD"
DIGEST = "d" * 64


def test_the_revision_is_0090_on_0089() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_revision("0090").down_revision == "0089"


def test_the_tables_come_and_go_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0089")
    assert not NEW & tables(database)
    assert "0090" in migrate(settings).applied
    assert tables(database) >= NEW
    command.downgrade(alembic_config(database), "0089")
    assert not NEW & tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    kind = conn.execute("SELECT key FROM work_kind LIMIT 1").fetchone()[0]
    conn.execute(
        "INSERT INTO work (id, kind, title, sort_title) VALUES (?, ?, 'T', 't')", (WORK, kind)
    )
    conn.execute(
        "INSERT INTO edition (id, work_id, lang, translation_type, media) "
        "VALUES (?, ?, 'en', 'fan', 'images')",
        (EDITION, WORK),
    )
    return conn


def _table(conn: sqlite3.Connection, ident: str = TABLE, **values: object) -> None:
    row = {
        "id": ident,
        "edition_id": EDITION,
        "work_id": WORK,
        "plugin_id": "p",
        "source_key": "s",
        "remote_id": "r",
        "digest": DIGEST,
        "held_digest": DIGEST,
        **values,
    }
    conn.execute(
        f"INSERT INTO source_migration ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",  # noqa: S608 - column names are the literal keys above
        tuple(row.values()),
    )


def test_a_table_has_a_known_state_a_named_source_and_two_fingerprints(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        for bad in (
            {"state": "half"},
            {"remote_id": ""},
            {"digest": "short"},
            {"held_digest": ""},
            {"number_offset": 100001},
            {"state": "applied"},  # applied is dated
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _table(conn, **bad)
        _table(conn)
        assert conn.execute("SELECT state, number_offset FROM source_migration").fetchone() == (
            "proposed",
            0,
        )
        with pytest.raises(sqlite3.IntegrityError):  # the Edition must exist
            _table(conn, SECOND, edition_id=SECOND)


def test_one_open_table_per_edition_and_fingerprint(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        _table(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _table(conn, SECOND)  # the same listing proposed twice is one table
        conn.execute("UPDATE source_migration SET state = 'cancelled' WHERE id = ?", (TABLE,))
        _table(conn, SECOND)  # once the first is closed, the same fingerprint may come again


def test_a_line_never_lands_twice_and_goes_with_its_table(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        _table(conn)
        add = (
            "INSERT INTO source_migration_row (migration_id, place, target_place, basis) "
            "VALUES (?, ?, ?, ?)"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (TABLE, "a", "t", "guess"))  # a known basis
        conn.execute(add, (TABLE, "main||11|", "main||1|", "offset"))
        with pytest.raises(sqlite3.IntegrityError):  # one line per number of the new source
            conn.execute(add, (TABLE, "main||11|", "main||2|", "offset"))
        with pytest.raises(sqlite3.IntegrityError):  # and one per place of the library
            conn.execute(add, (TABLE, "main||12|", "main||1|", "offset"))
        conn.execute(add, (TABLE, "main||12|", "main||2|", "offset"))
        conn.execute("DELETE FROM edition WHERE id = ?", (EDITION,))
        assert conn.execute("SELECT count(*) FROM source_migration").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM source_migration_row").fetchone()[0] == 0
