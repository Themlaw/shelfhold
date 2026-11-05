"""Migration 0007: one open download ticket per Variant (L18 review)."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from alembic import command

from tests.integration.test_schema_migration_0002 import alembic_config


@pytest.mark.req("EF-SRC-009")
def test_upgrade_adds_and_downgrade_drops_the_open_variant_index(tmp_path: Path) -> None:
    database = tmp_path / "a.db"
    config = alembic_config(database)
    command.upgrade(config, "0006")
    command.upgrade(config, "0007")
    with closing(sqlite3.connect(database)) as db:
        names = {r[1] for r in db.execute("PRAGMA index_list('download_request')")}
        assert "uq_download_request_open_variant" in names
    command.downgrade(config, "0006")
    with closing(sqlite3.connect(database)) as db:
        names = {r[1] for r in db.execute("PRAGMA index_list('download_request')")}
        assert "uq_download_request_open_variant" not in names
