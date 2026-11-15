"""Migration 0079 (M4 L23, EF-BIB-032): the table `external_link`.

One table, nothing else touched. The DDL parity of the whole database is
`test_migrated_ddl_equals_create_all`.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-BIB-032")


def test_single_alembic_head_with_0079_on_0077() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0079"]
    assert script.get_revision("0079").down_revision == "0077"


def test_upgrade_adds_the_table_and_downgrade_drops_it(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0077")
    assert "external_link" not in tables(database)
    assert "0079" in migrate(settings).applied
    assert "external_link" in tables(database)
    command.downgrade(alembic_config(database), "0077")
    assert "external_link" not in tables(database)


def test_the_table_refuses_a_url_that_is_not_https(settings: Settings) -> None:
    migrate(settings)
    with closing(sqlite3.connect(settings.database_path, isolation_level=None)) as conn:
        for url in ("javascript:alert(1)", "http://example.org/x"):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO external_link (id, entity_type, entity_id, url, provider, "
                    "fetched_at) VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZA', 'edition', "
                    "'01HZZZZZZZZZZZZZZZZZZZZZZB', ?, 'anilist', '2026-11-15 09:00:00')",
                    (url,),
                )
