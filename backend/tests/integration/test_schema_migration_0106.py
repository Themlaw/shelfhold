"""Migration 0106 (M5 L22a, EF-LEC-014): `reading_progress` reads `unit_coverage.extent`.

The view holds no data and its columns do not change (frozen since 1.1): only what it says of a
Unit that a file covers in part does. Reversible. The head of the migrations is pinned here, by the
newest one.
"""

import sqlite3
from contextlib import closing

import pytest
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import default_script_location, migrate
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("ENF-OPS-002", "EF-LEC-014")


def test_the_revision_is_0106_the_head_on_0104() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert len(script.get_heads()) == 1  # one head, whichever later lot moved it
    # 0105 is L21's: chained on 0104, re-chained if another lot lands first
    assert script.get_revision("0106").down_revision == "0104"


def _views(settings: Settings) -> dict[str, tuple[str, list[str]]]:
    with closing(sqlite3.connect(settings.database_path)) as db:
        out = {}
        for name in ("reading_progress", "reading_progress_proven"):
            sql = db.execute("SELECT sql FROM sqlite_master WHERE name = ?", (name,)).fetchone()[0]
            out[name] = (str(sql), [r[1] for r in db.execute(f"PRAGMA table_info({name})")])
        return out


def test_both_views_change_their_rule_and_not_their_columns_both_ways(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(settings.database_path), "0104")
    before = _views(settings)
    assert all("extent" not in sql for sql, _ in before.values())
    assert "0106" in migrate(settings).applied
    after = _views(settings)
    for name, (sql, columns) in after.items():
        assert "uc.extent <> 'full'" in sql and "o.extent = 'full'" in sql, name
        assert columns == before[name][1], name  # frozen since 1.1
    command.downgrade(alembic_config(settings.database_path), "0104")
    assert _views(settings) == before


def test_the_migrated_views_are_the_ones_the_schema_declares(settings: Settings) -> None:
    """The migration holds a literal copy of the SELECT (a migration never imports the schema):
    this is what keeps the two from drifting."""
    from shelfhold.infrastructure.schema.reading import (
        READING_PROGRESS_PROVEN_SELECT,
        READING_PROGRESS_SELECT,
    )

    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    views = _views(settings)

    def squash(text: str) -> str:
        return " ".join(text.split())

    assert squash(READING_PROGRESS_SELECT) in squash(views["reading_progress"][0])
    assert squash(READING_PROGRESS_PROVEN_SELECT) in squash(views["reading_progress_proven"][0])
