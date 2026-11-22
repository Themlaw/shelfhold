"""Migration 0110 (M6 L07, EF-SRC-017): the plugin repositories and the revocations seen.

Two tables, nothing else touched, reversible. `plugin_repo` pins a signed index by URL and by
the public key the admin gave (its `key_id` is the fingerprint shown everywhere); `last_version`
is the anti-rollback floor and starts at zero — no repository is preconfigured. A revocation seen
in an index stays seen (`plugin_revocation`), even if a later index omits it. The head of the
migrations is pinned here, by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-SRC-017")

TABLES = {"plugin_repo", "plugin_revocation"}


def _tables(database: object) -> set[str]:
    with closing(sqlite3.connect(str(database))) as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in rows}


def test_the_revision_is_0110_the_head_on_0108() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0110"]  # the head is pinned here, by the newest
    assert script.get_revision("0110").down_revision == "0108"


def test_the_tables_come_and_go_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0108")
    assert not TABLES & _tables(database)
    assert "0110" in migrate(settings).applied
    assert _tables(database) >= TABLES
    command.downgrade(alembic_config(database), "0108")
    assert not TABLES & _tables(database)


def test_zero_repo_preconfigured_and_the_checks_hold(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        assert conn.execute("SELECT count(*) FROM plugin_repo").fetchone() == (0,)
        conn.execute(
            "INSERT INTO plugin_repo (url, pubkey, key_id, last_version) "
            "VALUES ('https://index.example.org/index.json', 'RW...', 'ABCD1234', 3)"
        )
        for url, pubkey, key_id, last_version in (
            ("https://a.example.org/", "", "ABCD1234", 0),  # a pin without its key is nothing
            ("https://b.example.org/", "RW...", "", 0),  # nor without the fingerprint shown
            ("https://c.example.org/", "RW...", "ABCD1234", -1),  # the floor never goes below 0
        ):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO plugin_repo (url, pubkey, key_id, last_version) "
                    "VALUES (?, ?, ?, ?)",
                    (url, pubkey, key_id, last_version),
                )
        conn.execute("INSERT INTO plugin_revocation (kind, value) VALUES ('plugin_key', 'acme')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(  # the kind is a closed list: a wheel is revoked by its sha256
                "INSERT INTO plugin_revocation (kind, value) VALUES ('wheel', 'sha256:00')"
            )
        with pytest.raises(sqlite3.IntegrityError):  # one row per revocation
            conn.execute(
                "INSERT INTO plugin_revocation (kind, value) VALUES ('plugin_key', 'acme')"
            )
