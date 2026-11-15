"""Migration 0071 (M4 L20): what the metadata providers keep in the database.

`provider_quota.announced_limit` (the rate a provider last announced, shared by every worker) and
`provider_release_calendar` (the cached release calendar of a series). The DDL parity of the whole
database is `test_migrated_ddl_equals_create_all`: it runs at head and covers this revision.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config, tables


@pytest.mark.req("ENF-OPS-002", "ENF-LEG-005")
def test_single_alembic_head_with_0071_on_0064() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1
    assert script.get_revision("0071").down_revision == "0064"


@pytest.mark.req("ENF-OPS-002", "ENF-LEG-005")
def test_upgrade_from_0064_keeps_the_balances(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0064")
    with closing(sqlite3.connect(database, isolation_level=None)) as conn:
        conn.execute(
            "INSERT INTO provider_quota (provider, tokens, refilled_at) "
            "VALUES ('openlibrary', 1.0, '2026-11-01 10:00:00')"
        )
    assert "0071" in migrate(settings).applied
    with closing(sqlite3.connect(database)) as conn:
        row = conn.execute(
            "SELECT tokens, announced_limit FROM provider_quota WHERE provider = 'openlibrary'"
        ).fetchone()
    assert row == (1.0, None)  # the old row stays, with nothing announced
    assert "provider_release_calendar" in tables(database)


@pytest.mark.req("ENF-OPS-002")
def test_downgrade_to_0064_drops_what_0071_added(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    command.downgrade(alembic_config(database), "0064")
    assert "provider_release_calendar" not in tables(database)
    with closing(sqlite3.connect(database)) as conn:
        assert "announced_limit" not in {r[1] for r in conn.execute("PRAGMA table_info(provider_quota)")}
