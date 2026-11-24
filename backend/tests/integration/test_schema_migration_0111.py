"""Migration 0111 (M6 L08, EF-SRC-017 passe 8): the signer pin of `plugin_install`.

Two columns, `index_url` and `signer_pubkey`, both NULL for every plugin installed before (a
wheel the admin gave by hand is pinned by the admin's own act, not by a signature), and the
`pin_paired` check: an install is pinned or it is not — the repository and its signer come and go
together. The head of the migrations is pinned here, by the newest one.
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

COLUMNS = ("index_url", "signer_pubkey")


def _columns(database: str) -> list[str]:
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        return [row[1] for row in conn.execute("PRAGMA table_info(plugin_install)")]


def _table_sql(database: str) -> str:
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'plugin_install'"
        ).fetchone()[0]


def test_the_revision_is_0111_the_head_on_0110() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # one head, whichever later lot moved it
    assert script.get_revision("0111").down_revision == "0110"


def test_the_columns_come_and_go_with_the_migration(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0110")
    assert not [c for c in _columns(database) if c in COLUMNS]
    assert "0111" in migrate(settings).applied
    assert [c for c in _columns(database) if c in COLUMNS] == list(COLUMNS)
    command.downgrade(alembic_config(database), "0110")
    assert not [c for c in _columns(database) if c in COLUMNS]


def test_the_pin_is_paired_and_starts_empty(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        assert "CONSTRAINT ck_plugin_install_pin_paired" in _table_sql(str(settings.database_path))
        conn.execute(
            "INSERT INTO plugin_install (plugin_id, version, api, origin, sha256) "
            "VALUES ('acme', '1.0', '2', 'local', 'sha256:" + "a" * 64 + "')"
        )
        assert conn.execute("SELECT index_url, signer_pubkey FROM plugin_install").fetchone() == (
            None,
            None,
        )
        with pytest.raises(sqlite3.IntegrityError):  # a signer without its repository
            conn.execute("UPDATE plugin_install SET signer_pubkey = 'RW…' WHERE plugin_id = 'acme'")
        with pytest.raises(sqlite3.IntegrityError):  # a repository without its signer
            conn.execute(
                "UPDATE plugin_install SET index_url = 'https://index.example.org/index.json' "
                "WHERE plugin_id = 'acme'"
            )
        conn.execute(  # both at once: the pin of an install from an index
            "UPDATE plugin_install SET index_url = 'https://index.example.org/index.json', "
            "signer_pubkey = 'RW…' WHERE plugin_id = 'acme'"
        )
