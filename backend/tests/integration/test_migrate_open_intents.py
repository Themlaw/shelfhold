"""ENF-OPS-019: `shelfhold migrate` refuses to run while intents are open."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import (
    OpenIntentsError,
    current_revision,
    head_revision,
    migrate,
)
from tests.integration.test_migrations import CREATE_ONLY, scripts_with_extra_revision

INSERT = (
    "INSERT INTO fs_intent (id, op, intent_schema_version, src_path, dst_path, state, epoch) "
    "VALUES (?, 'rename', '1', '/library/manga/a', '/library/manga/b', ?, 0)"
)


def add_intent(settings: Settings, ident: str, state: str) -> None:
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute(INSERT, (ident, state))
        db.commit()


@pytest.mark.req("ENF-OPS-019")
@pytest.mark.parametrize("state", ["pending", "applying"])
def test_migrate_refuses_with_open_intents(settings: Settings, tmp_path: Path, state: str) -> None:
    migrate(settings)
    add_intent(settings, "01JBZ3X5QH8M2N4P6R7S9T0V1W", state)
    scripts = scripts_with_extra_revision(tmp_path, CREATE_ONLY)  # a migration is waiting
    revision_before = current_revision(settings.database_path)
    with pytest.raises(OpenIntentsError) as raised:
        migrate(settings, script_location=scripts)
    assert raised.value.count == 1
    assert "démarrer une fois la version actuelle" in str(raised.value)
    assert current_revision(settings.database_path) == revision_before == head_revision()


@pytest.mark.req("ENF-OPS-019")
def test_resolved_intents_do_not_block_migrate(settings: Settings, tmp_path: Path) -> None:
    migrate(settings)
    for i, state in enumerate(["done", "cancelled", "stale_from_backup"]):
        add_intent(settings, f"01JBZ3X5QH8M2N4P6R7S9T0V1{i}", state)
    scripts = scripts_with_extra_revision(tmp_path, CREATE_ONLY)
    result = migrate(settings, script_location=scripts)
    assert len(result.applied) == 1


@pytest.mark.req("ENF-OPS-019")
def test_migrate_on_a_fresh_database_has_no_intent_to_check(settings: Settings) -> None:
    assert migrate(settings).to_revision == head_revision()
