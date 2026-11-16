"""Migration 0085 (M4 L53, ADR-0054): `page_issue`, the chapters whose pages are set aside.

One table that goes with its Unit; nothing else is touched; reversible. The head of the migrations
is pinned here, by the newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-EXP-046")


def test_the_revision_is_0085_and_the_only_head() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0085"]
    assert script.get_revision("0085").down_revision == "0084"


def _tables(settings: Settings) -> set[str]:
    with closing(sqlite3.connect(settings.database_path)) as db:
        return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def test_the_table_comes_and_goes_with_the_migration(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), "0084")
    assert "page_issue" not in _tables(settings)
    assert "0085" in migrate(settings).applied
    assert "page_issue" in _tables(settings)
    command.downgrade(alembic_config(settings.database_path), "0084")
    assert "page_issue" not in _tables(settings)
