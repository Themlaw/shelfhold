"""Migration 0002 (platform, identity, coherence): single head, metadata parity, 0001 upgrade."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory

from shelfhold.config import Settings
from shelfhold.infrastructure.migrate import (
    current_revision,
    default_script_location,
    head_revision,
    migrate,
)
from shelfhold.infrastructure.schema import metadata

PLATFORM_TABLES = {
    "app_setting", "user", "session", "api_key", "audit", "idempotency_key",
    "instance", "physical_root", "mount_sentinel",
    "fs_intent", "resource_lock", "export_plan", "export_op",
    "schedule", "job_view", "domain_slot", "event", "notification_pending",
    "secret_key", "tombstone_kid", "content_tombstone", "tombstone_pending_root",
}  # fmt: skip


def alembic_config(database: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", str(default_script_location()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    return config


def tables(database: Path) -> set[str]:
    with closing(sqlite3.connect(database)) as db:
        rows = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        return {row[0] for row in rows} - {"alembic_version", "sqlite_sequence"}


@pytest.mark.req("ENF-OPS-002")
def test_single_alembic_head() -> None:
    script = ScriptDirectory(str(default_script_location()))
    assert script.get_heads() == ["0002"]
    assert head_revision() == "0002"
    chain = [rev.revision for rev in script.walk_revisions()]
    assert chain == ["0002", "0001"]  # linear, newest first


@pytest.mark.req("ENF-OPS-002", "ENF-OPS-020")
def test_metadata_matches_migrations(tmp_path: Path) -> None:
    database = tmp_path / "parity.db"
    command.upgrade(alembic_config(database), "head")
    engine = sa.create_engine(f"sqlite:///{database}")
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(
                connection,
                opts={
                    "compare_type": True,
                    "compare_server_default": True,
                    "render_as_batch": True,
                },
            )
            assert compare_metadata(context, metadata) == []
    finally:
        engine.dispose()
    assert tables(database) == {t.name for t in metadata.sorted_tables} == PLATFORM_TABLES


@pytest.mark.req("ENF-OPS-002")
def test_upgrade_from_0001_keeps_settings(settings: Settings) -> None:
    database = settings.database_path
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    command.upgrade(alembic_config(database), "0001")
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO app_setting (key, value) VALUES ('log_level', 'DEBUG')")
        db.commit()
    assert current_revision(database) == "0001"

    result = migrate(settings)

    assert result.applied == ("0002",)
    assert result.backup is not None
    assert "0001-to-0002" in result.backup.name
    assert current_revision(database) == "0002"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT key, value FROM app_setting").fetchall() == [
            ("log_level", "DEBUG")
        ]
    assert tables(database) == PLATFORM_TABLES


@pytest.mark.req("ENF-OPS-002")
def test_downgrade_to_0001_is_reversible_and_keeps_settings(tmp_path: Path) -> None:
    database = tmp_path / "down.db"
    config = alembic_config(database)
    command.upgrade(config, "0001")
    with closing(sqlite3.connect(database)) as db:
        db.execute("INSERT INTO app_setting (key, value) VALUES ('a', 'b')")
        db.commit()
    command.upgrade(config, "head")
    command.downgrade(config, "0001")
    assert tables(database) == {"app_setting"}
    assert current_revision(database) == "0001"
    with closing(sqlite3.connect(database)) as db:
        assert db.execute("SELECT * FROM app_setting").fetchall() == [("a", "b")]
    command.upgrade(config, "head")  # and back up again
    assert tables(database) == PLATFORM_TABLES


@pytest.mark.req("EF-INT-002")
def test_event_rows_are_append_only_in_the_migrated_database(tmp_path: Path) -> None:
    database = tmp_path / "events.db"
    command.upgrade(alembic_config(database), "head")
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO event (type, schema_version, payload, occurred_at, visibility) "
            "VALUES ('unit.added', 1, '{}', '2026-11-04 12:00:00.000000', 'ops')"
        )
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            db.execute("UPDATE event SET type = 'other'")
        db.execute("DELETE FROM event")  # the 90-day purge stays possible (ENF-OPS-009)
        db.commit()


@pytest.mark.req("EF-BIB-053")
def test_migrated_database_enforces_foreign_keys_when_opened_by_the_app(settings: Settings) -> None:
    migrate(settings)
    from shelfhold.infrastructure.db import create_db_engine

    engine = create_db_engine(settings.database_path)
    try:
        with engine.begin() as connection, pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "INSERT INTO notification_pending (id, event_id, channel, attempts, "
                    "next_try_at) VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAV', 999, 'x', 0, "
                    "'2026-11-04 12:00:00.000000')"
                )
            )
    finally:
        engine.dispose()


def normalized_ddl(database: Path) -> dict[str, str]:
    """`sqlite_master` statements by object, whitespace- and member-order-insensitive.

    Alembic does not compare CHECK constraints or triggers; this does, so a drifting
    expression in either the metadata or the migration is caught.
    """
    import re

    with closing(sqlite3.connect(database)) as db:
        rows = db.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' AND name <> 'alembic_version'"
        ).fetchall()
    result: dict[str, str] = {}
    for kind, name, sql in rows:
        text = re.sub(r"\s+", " ", sql.replace('"', "")).strip().lower()
        if kind == "table":
            head, _, rest = text.partition("(")
            body = rest.rsplit(")", 1)[0]
            parts, depth, current = [], 0, ""
            for char in body:
                depth += char == "("
                depth -= char == ")"
                if char == "," and depth == 0:
                    parts.append(current.strip())
                    current = ""
                else:
                    current += char
            parts.append(current.strip())
            text = head + "(" + " | ".join(sorted(parts)) + ")"
        result[f"{kind}:{name}"] = text
    return result


@pytest.mark.req("ENF-OPS-002")
def test_migrated_ddl_equals_create_all(tmp_path: Path) -> None:
    migrated = tmp_path / "migrated.db"
    command.upgrade(alembic_config(migrated), "head")
    created = tmp_path / "created.db"
    engine = sa.create_engine(f"sqlite:///{created}")
    try:
        metadata.create_all(engine)
    finally:
        engine.dispose()
    from_migrations, from_metadata = normalized_ddl(migrated), normalized_ddl(created)
    assert from_migrations.keys() == from_metadata.keys()
    assert any(key.startswith("trigger:") for key in from_metadata)
    for key in from_metadata:
        assert from_migrations[key] == from_metadata[key], key
