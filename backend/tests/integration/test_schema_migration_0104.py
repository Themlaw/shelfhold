"""Migration 0104 (M5 L20a, EF-BIB-015): `rendition`.

A converted image, addressed by the hash of its source, its kind and its parameters. Hashes and a
closed list of kinds only; nothing existing is touched; reversible."""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-015")

SOURCE = "sha256:" + "a" * 64
OUT = "sha256:" + "b" * 64


def test_the_revision_is_0104_the_head_on_0102() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0104"]  # the head is pinned here, by the newest
    assert script.get_revision("0104").down_revision == "0102"


def test_the_table_comes_and_goes_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0102")
    assert "rendition" not in tables(database)
    assert "0104" in migrate(settings).applied
    assert "rendition" in tables(database)
    command.downgrade(alembic_config(database), "0102")
    assert "rendition" not in tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    return sqlite3.connect(settings.database_path, isolation_level=None)


def _insert(conn: sqlite3.Connection, **over: object) -> None:
    v = {"source": SOURCE, "kind": "jpeg", "params": "{}", "out": OUT, "size": 10} | over
    conn.execute(
        "INSERT INTO rendition (source_content_hash, kind, params, content_hash, size) "
        "VALUES (?, ?, ?, ?, ?)",
        (v["source"], v["kind"], v["params"], v["out"], v["size"]),
    )


def test_a_rendition_is_one_per_source_kind_and_params(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        _insert(conn)
        with pytest.raises(sqlite3.IntegrityError):
            _insert(conn, out="sha256:" + "c" * 64)
        _insert(conn, kind="png")
        _insert(conn, params='{"entry":"1.webp"}')
        assert conn.execute("SELECT COUNT(*) FROM rendition").fetchone() == (3,)
        row = conn.execute("SELECT created_at FROM rendition LIMIT 1").fetchone()
        assert row[0]  # stamped by the database


def test_the_ledger_holds_hashes_closed_kinds_and_json_objects_only(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        for bad in (
            {"source": "../x"}, {"source": "deadbeef"}, {"out": "/etc/passwd"}, {"kind": "gif"},
            {"kind": "webp"}, {"size": 0}, {"size": -1}, {"params": "[]"}, {"params": "nope"},
        ):  # fmt: skip
            with pytest.raises(sqlite3.IntegrityError):
                _insert(conn, **bad)
