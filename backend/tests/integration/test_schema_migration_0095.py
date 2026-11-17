"""Migration 0095 (M4 L26, EF-INT-008): `tachibk_import`, `read_marker` and `tracker_hint`.

One preview-then-apply batch per imported backup, the markers of reading a profile brought with it
(waiting for their series to be resolved), and the hints of the trackers it used, with no secret.
Nothing existing is touched; reversible.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-INT-008")

NEW = {"tachibk_import", "read_marker", "tracker_hint"}
USER = "01HZZZZZZZZZZZZZZZZZZZZZZB"
BATCH = "01HZZZZZZZZZZZZZZZZZZZZZZA"
ITEM = "01HZZZZZZZZZZZZZZZZZZZZZZC"
MARKER = "01HZZZZZZZZZZZZZZZZZZZZZZD"
HINT = "01HZZZZZZZZZZZZZZZZZZZZZZE"


def test_the_revision_is_0095_the_head_on_0094() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0095"]  # the head is pinned here, by the newest
    assert script.get_revision("0095").down_revision == "0094"


def test_the_tables_come_and_go_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0094")
    assert not NEW & tables(database)
    assert "0095" in migrate(settings).applied
    assert tables(database) >= NEW
    command.downgrade(alembic_config(database), "0094")
    assert not NEW & tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO user (id, username, role, can_rate, max_age_rating) "
        "VALUES (?, 'ann', 'member', 0, NULL)",
        (USER,),
    )
    conn.execute(
        "INSERT INTO pending_source_item (id, source_key, series_ref, title, fetched_at) "
        "VALUES (?, 'mihon:1', '/s', 'T', '2026-11-17 00:00:00')",
        (ITEM,),
    )
    return conn


def _batch(conn: sqlite3.Connection, state: str = "previewed") -> None:
    conn.execute(
        "INSERT INTO tachibk_import (id, user_id, schema_version, content_hash, state) "
        "VALUES (?, ?, 'mihon-1', ?, ?)",
        (BATCH, USER, "a" * 64, state),
    )


def test_a_batch_has_a_state_a_hash_and_goes_with_its_profile(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            _batch(conn, "half-applied")  # a closed list of states
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tachibk_import (id, user_id, schema_version, content_hash) "
                "VALUES (?, ?, 'mihon-1', 'short')",
                (BATCH, USER),
            )
        _batch(conn)
        conn.execute("DELETE FROM user WHERE id = ?", (USER,))
        assert conn.execute("SELECT COUNT(*) FROM tachibk_import").fetchone() == (0,)


def test_a_marker_is_one_chapter_of_one_series_for_one_profile(settings: Settings) -> None:
    add = (
        'INSERT INTO read_marker (id, user_id, pending_item_id, chapter_ref, "read", last_page) '
        "VALUES (?, ?, ?, ?, ?, ?)"
    )
    with closing(_connect(settings)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (MARKER, USER, ITEM, "", 1, None))  # a chapter has a reference
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (MARKER, USER, ITEM, "/c/1", 2, None))  # a boolean is 0 or 1
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (MARKER, USER, ITEM, "/c/1", 0, -1))  # a page is not negative
        conn.execute(add, (MARKER, USER, ITEM, "/c/1", 1, None))
        with pytest.raises(sqlite3.IntegrityError):  # once per profile, series and chapter
            conn.execute(add, ("01HZZZZZZZZZZZZZZZZZZZZZZF", USER, ITEM, "/c/1", 0, 3))


_ADD_MARKER = (
    'INSERT INTO read_marker (id, user_id, pending_item_id, chapter_ref, "read") '
    "VALUES (?, ?, ?, '/c/1', 1)"
)


def test_a_marker_goes_with_its_series(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        conn.execute(_ADD_MARKER, (MARKER, USER, ITEM))
        conn.execute("DELETE FROM pending_source_item WHERE id = ?", (ITEM,))
        assert conn.execute("SELECT COUNT(*) FROM read_marker").fetchone() == (0,)


def test_a_marker_goes_with_its_profile(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        conn.execute(_ADD_MARKER, (MARKER, USER, ITEM))
        conn.execute("DELETE FROM user WHERE id = ?", (USER,))
        assert conn.execute("SELECT COUNT(*) FROM read_marker").fetchone() == (0,)


def test_a_hint_names_a_tracker_from_a_closed_list_and_holds_no_address(settings: Settings) -> None:
    add = (
        "INSERT INTO tracker_hint (id, user_id, pending_item_id, tracker, remote_id) "
        "VALUES (?, ?, ?, ?, ?)"
    )
    with closing(_connect(settings)) as conn:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (HINT, USER, ITEM, "somewhere", "1"))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(add, (HINT, USER, ITEM, "anilist", "x" * 65))  # an identifier, not a URL
        conn.execute(add, (HINT, USER, ITEM, "anilist", "7001"))
        with pytest.raises(sqlite3.IntegrityError):  # one entry per tracker, profile and series
            conn.execute(add, ("01HZZZZZZZZZZZZZZZZZZZZZZF", USER, ITEM, "anilist", "7002"))
        columns = {r[1] for r in conn.execute("PRAGMA table_info(tracker_hint)")}
        assert not {c for c in columns if "url" in c or "token" in c or "secret" in c}
