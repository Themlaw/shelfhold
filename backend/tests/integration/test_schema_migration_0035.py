"""Migration 0035 (M2 L27, EF-SRC-037, I52): one open request per Unit, whatever the writers.

`download_request.unit_id` is filled from the Variant, a Unit that already holds two open requests
keeps the older and cancels the other, and a unique index says no to a second one."""

import sqlite3
from contextlib import closing
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import default_script_location, migrate
from shelfhold.infrastructure.schema import acquisition
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)
from tests.integration.test_schema_migration_0002 import alembic_config

pytestmark = pytest.mark.req("EF-SRC-037", "ENF-OPS-002")


def _previous() -> str:
    down = ScriptDirectory(str(default_script_location())).get_revision("0035").down_revision
    assert isinstance(down, str)
    return down


def test_upgrade_fills_unit_id_dedupes_open_requests_and_adds_the_index(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), _previous())
    engine = create_db_engine(database)
    now = datetime(2026, 11, 11, tzinfo=UTC)
    with engine.begin() as connection:
        work = add_work(connection)
        edition = add_edition(connection, work)
        first = add_binding(connection, work, edition, add_source_item(connection, "a"))
        second = add_binding(
            connection, work, edition, add_source_item(connection, "b"), priority=2
        )
        unit = add_unit(connection, edition, "1")
        lone = add_unit(connection, edition, "2")
        ids = []
        for binding, target in ((first, unit), (second, unit), (first, lone)):
            variant = add_variant(connection, target, binding, f"r-{new_ulid()}")
            request = new_ulid()
            ids.append(request)
            connection.execute(
                sa.insert(acquisition.download_request).values(
                    id=request, target_kind="unit", variant_id=variant,
                    source_binding_id=binding, state="requested", requested_at=now,
                )
            )  # fmt: skip
    engine.dispose()

    assert "0035" in migrate(settings).applied

    with closing(sqlite3.connect(database)) as db:
        rows = dict(db.execute("SELECT id, state FROM download_request"))
        assert [rows[i] for i in ids] == ["requested", "cancelled", "requested"]  # the older stays
        units = {r[0]: r[1] for r in db.execute("SELECT id, unit_id FROM download_request")}
        assert units[ids[0]] == unit and units[ids[2]] == lone
        with pytest.raises(sqlite3.IntegrityError):  # the wall
            db.execute(
                "INSERT INTO download_request (id, target_kind, variant_id, source_binding_id, "
                "unit_id, state) SELECT ?, 'unit', variant_id, source_binding_id, unit_id, "
                "'requested' FROM download_request WHERE id = ?",
                (new_ulid(), ids[2]),
            )

    command.downgrade(alembic_config(database), _previous())
    with closing(sqlite3.connect(database)) as db:
        columns = [r[1] for r in db.execute("PRAGMA table_info(download_request)")]
        assert "unit_id" not in columns
