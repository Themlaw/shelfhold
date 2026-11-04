"""ENF-OPS-002: shelfhold migrate, pre-migration backup, refusal on a newer schema."""

import hashlib
import json
import shutil
import sqlite3
import stat
import time
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.logging import configure_logging
from shelfhold.infrastructure.migrate import (
    MigrationError,
    SchemaTooNewError,
    current_revision,
    default_script_location,
    head_revision,
    migrate,
)
from shelfhold.main import create_app

MIGRATION_BUDGET_SECONDS = 600
REVISION_TEMPLATE = '''"""test revision"""

from alembic import op

revision = "9000"
down_revision = "{down}"
branch_labels = None
depends_on = None


def upgrade() -> None:
    {body}


def downgrade() -> None:
    pass
'''


def write_extra_revision(scripts: Path, body: str) -> None:
    indented = body.replace("\n", "\n    ")
    (scripts / "versions" / "9000_extra.py").write_text(
        REVISION_TEMPLATE.format(body=indented, down=head_revision()), encoding="utf-8"
    )


def scripts_with_extra_revision(tmp_path: Path, body: str) -> Path:
    destination = tmp_path / "scripts"
    shutil.copytree(
        default_script_location(), destination, ignore=shutil.ignore_patterns("__pycache__")
    )
    write_extra_revision(destination, body)
    return destination


CREATE_THEN_FAIL = "op.execute('CREATE TABLE extra (id INTEGER)')\nraise RuntimeError('boom')"
CREATE_ONLY = "op.execute('CREATE TABLE extra (id INTEGER)')"


def table_names(path: Path) -> set[str]:
    with closing(sqlite3.connect(path)) as db:
        return {row[0] for row in db.execute("SELECT name FROM sqlite_master")}


@pytest.mark.req("ENF-OPS-002")
def test_migrate_creates_schema_at_head(settings: Settings) -> None:
    result = migrate(settings)
    assert result.to_revision == head_revision()
    assert current_revision(settings.database_path) == head_revision()
    assert result.backup is None  # nothing to save on a fresh database
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


@pytest.mark.req("ENF-OPS-002")
def test_migration_fits_the_ten_minute_budget(settings: Settings) -> None:
    # ENF-OPS-002: budget <= 10 min on the reference profile. At M0 the schema is the
    # initial one, so this guards the order of magnitude; the volume profile comes with M1a.
    started = time.monotonic()
    migrate(settings)
    assert time.monotonic() - started < MIGRATION_BUDGET_SECONDS


@pytest.mark.req("ENF-OPS-002")
def test_migrate_command_idempotent_on_double_run(settings: Settings) -> None:
    first = migrate(settings)
    second = migrate(settings)
    assert first.applied
    assert second.applied == ()
    assert second.backup is None
    assert current_revision(settings.database_path) == head_revision()
    assert not (settings.data_dir / "backups").exists()


@pytest.mark.req("ENF-OPS-002")
def test_broken_migration_aborts_with_explicit_log(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="INFO", log_format="json")
    migrate(settings)
    scripts = scripts_with_extra_revision(tmp_path, CREATE_THEN_FAIL)
    with pytest.raises(MigrationError, match="9000"):
        migrate(settings, script_location=scripts)
    records = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    failed = [r for r in records if r["event"] == "migration_failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "error"
    assert failed[0]["revision"] == "9000"
    assert "boom" in failed[0]["error"]
    assert failed[0]["backup"].endswith(".db")
    assert any(r["event"] == "migration_restored" for r in records)
    # The half-applied DDL is gone: the database is exactly what it was.
    assert current_revision(settings.database_path) == head_revision()
    assert "extra" not in table_names(settings.database_path)
    assert settings.database_path.stat().st_mode & 0o777 == 0o600  # restore keeps it private
    # Once the revision is fixed, the next run succeeds.
    write_extra_revision(scripts, CREATE_ONLY)
    result = migrate(settings, script_location=scripts)
    assert result.applied == ("9000",)
    assert "extra" in table_names(settings.database_path)


@pytest.mark.req("ENF-OPS-002")
def test_failed_first_migration_leaves_no_database(settings: Settings, tmp_path: Path) -> None:
    scripts = scripts_with_extra_revision(tmp_path, CREATE_THEN_FAIL)
    with pytest.raises(MigrationError):
        migrate(settings, script_location=scripts)
    assert not settings.database_path.exists()
    assert not list(settings.data_dir.glob("shelfhold.db*"))
    write_extra_revision(scripts, CREATE_ONLY)
    assert migrate(settings, script_location=scripts).to_revision == "9000"


@pytest.mark.req("ENF-OPS-002")
def test_rerun_after_failure_keeps_the_original_backup_intact(
    settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    scripts = scripts_with_extra_revision(tmp_path, CREATE_THEN_FAIL)
    directory = settings.data_dir / "backups" / "pre-migration"
    with pytest.raises(MigrationError):
        migrate(settings, script_location=scripts)
    (original,) = directory.glob("*.db")
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    with pytest.raises(MigrationError):
        migrate(settings, script_location=scripts)
    assert hashlib.sha256(original.read_bytes()).hexdigest() == digest
    assert len(list(directory.glob("*.db"))) == 2  # the second run wrote its own copy


@pytest.mark.req("ENF-OPS-002")
def test_backup_is_private_and_written_atomically(settings: Settings, tmp_path: Path) -> None:
    migrate(settings)
    scripts = scripts_with_extra_revision(tmp_path, CREATE_ONLY)
    result = migrate(settings, script_location=scripts)
    assert result.backup is not None
    assert stat.S_IMODE(result.backup.stat().st_mode) == 0o600
    assert [p.name for p in result.backup.parent.iterdir()] == [result.backup.name]  # no .tmp


@pytest.mark.req("ENF-OPS-002")
def test_alembic_cli_fallback_uses_the_configured_url(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    database = tmp_path / "cli.db"
    config = Config()
    config.set_main_option("script_location", str(default_script_location()))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "head")
    assert current_revision(database) == head_revision()


@pytest.mark.req("ENF-OPS-002")
def test_backup_named_by_revision_taken_before_migration(
    settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    head = head_revision()
    scripts = scripts_with_extra_revision(tmp_path, "op.execute('CREATE TABLE extra (id INTEGER)')")
    result = migrate(settings, script_location=scripts)
    assert result.applied == ("9000",)
    assert result.backup is not None
    assert result.backup.parent == settings.data_dir / "backups" / "pre-migration"
    assert head in result.backup.name
    assert "9000" in result.backup.name
    assert current_revision(settings.database_path) == "9000"
    # The copy holds the schema as it was before the migration.
    assert current_revision(result.backup) == head
    with closing(sqlite3.connect(result.backup)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert "extra" not in tables


def stamp_future_revision(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        db.execute("INSERT INTO alembic_version VALUES ('from_the_future')")
        db.commit()


@pytest.mark.req("ENF-OPS-002")
def test_refuses_to_start_on_newer_schema(settings: Settings) -> None:
    stamp_future_revision(settings)
    with (
        pytest.raises(SchemaTooNewError, match="from_the_future"),
        TestClient(create_app(settings)),
    ):
        pass


@pytest.mark.req("ENF-OPS-002")
def test_migrate_refuses_a_newer_schema(settings: Settings) -> None:
    stamp_future_revision(settings)
    with pytest.raises(SchemaTooNewError):
        migrate(settings)
    assert not (settings.data_dir / "backups").exists()


BATCH_ON_USER = """\
with op.batch_alter_table('user') as batch:
    batch.alter_column('locale', server_default='en')"""
ORPHAN_SESSION = (
    'op.execute("INSERT INTO session (id, user_id, token_hash, csrf_secret, last_seen_at, '
    "auth_time, absolute_expiry) VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAV', "
    "'01ARZ3NDEKTSV4RRFFQ69G5FAW', 'sha256:" + "a" * 64 + "', 'x', '2026-01-01 00:00:00', "
    "'2026-01-01 00:00:00', '2026-01-01 00:00:00')\")"
)
USER_ID = "01ARZ3NDEKTSV4RRFFQ69G5FAX"


def seed_user_with_session(database: Path) -> None:
    stamp = "2026-01-01 00:00:00"
    with closing(sqlite3.connect(database)) as db:
        db.execute(
            "INSERT INTO user (id, username, password_hash, role, can_rate) "
            "VALUES (?, 'admin', '$argon2id$x', 'admin', 1)",
            (USER_ID,),
        )
        db.execute(
            "INSERT INTO session (id, user_id, token_hash, csrf_secret, last_seen_at, auth_time, "
            "absolute_expiry) VALUES ('01ARZ3NDEKTSV4RRFFQ69G5FAV', ?, ?, 'x', ?, ?, ?)",
            (USER_ID, "sha256:" + "a" * 64, stamp, stamp, stamp),
        )
        db.commit()


def count_rows(database: Path, table: str) -> int:
    with closing(sqlite3.connect(database)) as db:
        return int(db.execute(f"SELECT count(*) FROM {table}").fetchone()[0])  # noqa: S608


@pytest.mark.req("ENF-OPS-002")
def test_batch_rebuild_of_a_parent_table_keeps_its_children(
    settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    seed_user_with_session(settings.database_path)
    scripts = scripts_with_extra_revision(tmp_path, BATCH_ON_USER)
    result = migrate(settings, script_location=scripts)
    assert result.applied == ("9000",)
    assert count_rows(settings.database_path, "user") == 1
    assert (
        count_rows(settings.database_path, "session") == 1
    )  # no ON DELETE CASCADE from DROP TABLE
    # Foreign keys are enforced again for the application's own connections.
    engine = create_db_engine(settings.database_path)
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    finally:
        engine.dispose()


@pytest.mark.req("ENF-OPS-002")
def test_migration_leaving_a_foreign_key_violation_fails_and_restores(
    settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    scripts = scripts_with_extra_revision(tmp_path, ORPHAN_SESSION)
    with pytest.raises(MigrationError, match="foreign key"):
        migrate(settings, script_location=scripts)
    assert current_revision(settings.database_path) == head_revision()
    assert count_rows(settings.database_path, "session") == 0


@pytest.mark.req("ENF-OPS-002")
def test_alembic_cli_fallback_also_protects_children(tmp_path: Path) -> None:
    from alembic import command
    from alembic.config import Config

    database = tmp_path / "cli.db"
    scripts = scripts_with_extra_revision(tmp_path, BATCH_ON_USER)
    config = Config()
    config.set_main_option("script_location", str(scripts))
    config.set_main_option("sqlalchemy.url", f"sqlite:///{database}")
    command.upgrade(config, "0002")
    seed_user_with_session(database)
    command.upgrade(config, "head")
    assert count_rows(database, "session") == 1
