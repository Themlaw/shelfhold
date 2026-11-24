"""Migration 0112 (M6 L09a, EF-SRC-020, D15): the lifecycle columns and the orphaned binding.

`plugin_install` keeps the lifecycle of the installed third-party code: `pinned` (the version is
frozen) and the previous kept version (`previous_version`, `previous_sha256`, paired), the one the
reserved rollback returns to. `source_binding` admits one `disabled_reason` more: `orphaned`, the
mark of a binding whose plugin was uninstalled — the row stays, with everything it holds, and a
reinstall of the same key at the same signer reattaches it. The head of the migrations is pinned
here, by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-SRC-020")

COLUMNS = ("pinned", "previous_version", "previous_sha256")
REASONS = ("not_loaded", "content_failures", "removed_upstream", "manual", "orphaned")


def _columns(database: str) -> list[str]:
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        return [row[1] for row in conn.execute("PRAGMA table_info(plugin_install)")]


def _table_sql(database: str, table: str) -> str:
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()[0]


def _binding(database: str) -> tuple[object, ...]:
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        return conn.execute("SELECT active, disabled_reason FROM source_binding").fetchone()


def _seed(database: str) -> None:
    """A binding of a plugin's source, and the install row of that plugin."""
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO work_kind (key, label_fr, default_media) "
            "VALUES ('manga', 'manga', 'images')"
        )
        conn.execute(
            "INSERT INTO work (id, kind, title, sort_title) "
            "VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZW', 'manga', 'Un titre', 'un titre')"
        )
        conn.execute(
            "INSERT INTO edition (id, work_id, lang, translation_type, media) "
            "VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZE', "
            "'01HZZZZZZZZZZZZZZZZZZZZZZW', 'fr', 'official', 'images')"
        )
        conn.execute(
            "INSERT INTO source_item (id, plugin_id, source_key, remote_id) "
            "VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZS', 'acme', 'acme', 'series/1')"
        )
        conn.execute(
            "INSERT INTO source_binding "
            "(id, work_id, edition_id, source_item_id, priority, active) "
            "VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZB', "
            "'01HZZZZZZZZZZZZZZZZZZZZZZW', '01HZZZZZZZZZZZZZZZZZZZZZZE', "
            "'01HZZZZZZZZZZZZZZZZZZZZZZS', 1, 1)"
        )
        conn.execute(
            "INSERT INTO plugin_install (plugin_id, version, api, origin, sha256) "
            "VALUES ('acme', '1.0.0', '2', 'local', 'sha256:" + "a" * 64 + "')"
        )


def test_the_revision_is_0112_the_head_on_0115() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # the head is pinned by the newest, one chain only
    assert script.get_revision("0112").down_revision == "0115"


def test_the_lifecycle_columns_come_and_go_with_the_migration(settings: Settings) -> None:
    database = str(settings.database_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0115")
    assert not [c for c in _columns(database) if c in COLUMNS]
    assert "0112" in migrate(settings).applied
    assert [c for c in _columns(database) if c in COLUMNS] == list(COLUMNS)
    command.downgrade(alembic_config(database), "0115")
    assert not [c for c in _columns(database) if c in COLUMNS]


def test_the_checks_hold_and_the_previous_is_paired(settings: Settings) -> None:
    migrate(settings)
    database = str(settings.database_path)
    assert "CONSTRAINT ck_plugin_install_previous_paired" in _table_sql(database, "plugin_install")
    assert "orphaned" in _table_sql(database, "source_binding")  # the reason the migration adds
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO plugin_install (plugin_id, version, api, origin, sha256) "
            "VALUES ('acme', '1.0.0', '2', 'local', 'sha256:" + "a" * 64 + "')"
        )
        assert conn.execute("SELECT pinned, previous_version FROM plugin_install").fetchone() == (
            0,
            None,
        )
        with pytest.raises(sqlite3.IntegrityError):  # a previous version without its wheel hash
            conn.execute(
                "UPDATE plugin_install SET previous_version = '0.9.0' WHERE plugin_id = 'acme'"
            )
        with pytest.raises(sqlite3.IntegrityError):  # a wheel hash that is not a sha256
            conn.execute(
                "UPDATE plugin_install SET previous_version = '0.9.0', "
                "previous_sha256 = '0.9.0' WHERE plugin_id = 'acme'"
            )
        conn.execute(  # both at once: the previous kept version of the last update
            "UPDATE plugin_install SET previous_version = '0.9.0', "
            "previous_sha256 = 'sha256:" + "b" * 64 + "' WHERE plugin_id = 'acme'"
        )


def test_an_orphaned_binding_survives_the_downgrade(settings: Settings) -> None:
    database = str(settings.database_path)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0112")
    _seed(database)
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(  # the plugin went away: the binding is marked, not deleted
            "UPDATE source_binding SET active = 0, disabled_reason = 'orphaned'"
        )
    assert _binding(database) == (0, "orphaned")
    command.downgrade(alembic_config(database), "0115")
    # the mark becomes the closest reason the older schema knows; the row is still there
    assert _binding(database) == (0, "not_loaded")
