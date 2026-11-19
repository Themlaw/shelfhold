"""Migration 0108 (M5 L20b, EF-BIB-015): `rendition.kind` takes `kepub`.

The cache of L20a held two kinds of image; a KEPUB is a third. The table is rebuilt with the wider
closed list: its rows, its key and its other checks come through unchanged; reversible (a KEPUB row
is a regenerable cache and is dropped on the way back)."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-015")

SOURCE = "sha256:" + "a" * 64
OUT = "sha256:" + "b" * 64


def test_the_revision_is_0108_the_head_on_0105() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0108"]  # the head is pinned here, by the newest
    assert script.get_revision("0108").down_revision == "0105"


def _insert(conn: sqlite3.Connection, **over: object) -> None:
    v = {"source": SOURCE, "kind": "kepub", "params": "{}", "out": OUT, "size": 10} | over
    conn.execute(
        "INSERT INTO rendition (source_content_hash, kind, params, content_hash, size) "
        "VALUES (?, ?, ?, ?, ?)",
        (v["source"], v["kind"], v["params"], v["out"], v["size"]),
    )


def test_a_kepub_row_is_accepted_and_the_other_checks_stay(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        _insert(conn)
        _insert(conn, kind="jpeg")
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, out="sha256:" + "c" * 64)  # one row per source, kind and parameters
        for bad in (
            {"kind": "gif"}, {"kind": "epub"}, {"source": "../x"}, {"out": "/etc/passwd"},
            {"size": 0}, {"params": "[]"},
        ):  # fmt: skip
            with pytest.raises(sqlite3.IntegrityError):
                _insert(conn, **bad)


def test_rows_of_0105_come_through_and_the_way_back_drops_only_a_kepub(
    settings: Settings,
) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0105")
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        _insert(conn, kind="jpeg", params='{"quality":85}')
    assert "0108" in migrate(settings).applied
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        _insert(conn, kind="kepub")
        assert conn.execute("SELECT COUNT(*) FROM rendition").fetchone() == (2,)
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'ix_rendition_content_hash'"
        ).fetchone()
    command.downgrade(alembic_config(database), "0105")
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        assert conn.execute("SELECT kind FROM rendition").fetchall() == [("jpeg",)]
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, kind="kepub")
