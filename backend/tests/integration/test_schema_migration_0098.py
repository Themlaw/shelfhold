"""Migration 0098 (M4 L28, EF-BIB-023): `bulk_order`.

The selection a preview froze, for one admin, applied once by one job: identifiers and a digest, a
closed list of kinds and of states, and it goes with its user. Nothing existing is touched;
reversible.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-023")

USER = "01HZZZZZZZZZZZZZZZZZZZZZZB"
ORDER = "01HZZZZZZZZZZZZZZZZZZZZZZA"


def test_the_revision_is_0098_on_0097() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_revision("0098").down_revision == "0097"  # the head is pinned by the newest


def test_the_table_comes_and_goes_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0097")
    assert "bulk_order" not in tables(database)
    assert "0098" in migrate(settings).applied
    assert "bulk_order" in tables(database)
    command.downgrade(alembic_config(database), "0097")
    assert "bulk_order" not in tables(database)


def _connect(settings: Settings) -> sqlite3.Connection:
    migrate(settings)
    conn = sqlite3.connect(settings.database_path, isolation_level=None)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        "INSERT INTO user (id, username, role, can_rate) VALUES (?, 'ann', 'admin', 1)", (USER,)
    )
    return conn


def _order(conn: sqlite3.Connection, **over: str) -> None:
    values = {
        "kind": "rate", "state": "previewed", "params": '{"age": "13+"}',
        "work_ids": '["01HZZZZZZZZZZZZZZZZZZZZZZC"]', "digest": "d" * 64,
    } | over  # fmt: skip
    conn.execute(
        "INSERT INTO bulk_order (id, kind, actor_id, params, work_ids, digest, state) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ORDER, values["kind"], USER, values["params"], values["work_ids"], values["digest"],
         values["state"]),
    )  # fmt: skip


def test_an_order_has_a_closed_kind_a_closed_state_and_json_of_the_right_shape(
    settings: Settings,
) -> None:
    with closing(_connect(settings)) as conn:
        for bad in (
            {"kind": "delete"}, {"state": "half-applied"}, {"params": "[]"}, {"work_ids": "{}"},
            {"work_ids": "not json"},
        ):  # fmt: skip
            with pytest.raises(sqlite3.IntegrityError):
                _order(conn, **bad)
        _order(conn)
        row = conn.execute("SELECT state, job_id, summary FROM bulk_order").fetchone()
        assert row == ("previewed", None, "{}")


def test_an_order_goes_with_its_user(settings: Settings) -> None:
    with closing(_connect(settings)) as conn:
        _order(conn)
        conn.execute("DELETE FROM user WHERE id = ?", (USER,))
        assert conn.execute("SELECT COUNT(*) FROM bulk_order").fetchone() == (0,)
