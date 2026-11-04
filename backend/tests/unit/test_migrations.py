"""ENF-OPS-002: shelfhold migrate, pre-migration backup, refusal on a newer schema."""

import json
import shutil
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
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

REVISION_TEMPLATE = '''"""test revision"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    {body}


def downgrade() -> None:
    pass
'''


def scripts_with_extra_revision(tmp_path: Path, body: str) -> Path:
    destination = tmp_path / "scripts"
    shutil.copytree(
        default_script_location(), destination, ignore=shutil.ignore_patterns("__pycache__")
    )
    (destination / "versions" / "0002_extra.py").write_text(
        REVISION_TEMPLATE.format(body=body), encoding="utf-8"
    )
    return destination


def test_migrate_creates_schema_at_head(settings: Settings) -> None:
    result = migrate(settings)
    assert result.to_revision == head_revision()
    assert current_revision(settings.database_path) == head_revision()
    assert result.backup is None  # nothing to save on a fresh database
    with closing(sqlite3.connect(settings.database_path)) as db:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_migrate_command_idempotent_on_double_run(settings: Settings) -> None:
    first = migrate(settings)
    second = migrate(settings)
    assert first.applied
    assert second.applied == ()
    assert second.backup is None
    assert current_revision(settings.database_path) == head_revision()
    assert not (settings.data_dir / "backups").exists()


def test_broken_migration_aborts_with_explicit_log(
    settings: Settings, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    configure_logging(level="INFO", log_format="json")
    migrate(settings)
    scripts = scripts_with_extra_revision(tmp_path, "raise RuntimeError('boom')")
    with pytest.raises(MigrationError, match="0002"):
        migrate(settings, script_location=scripts)
    records = [
        json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")
    ]
    failed = [r for r in records if r["event"] == "migration_failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "error"
    assert failed[0]["revision"] == "0002"
    assert "boom" in failed[0]["error"]
    assert failed[0]["backup"].endswith(".db")
    assert current_revision(settings.database_path) == "0001"


def test_backup_named_by_revision_taken_before_migration(
    settings: Settings, tmp_path: Path
) -> None:
    migrate(settings)
    scripts = scripts_with_extra_revision(tmp_path, "op.execute('CREATE TABLE extra (id INTEGER)')")
    result = migrate(settings, script_location=scripts)
    assert result.applied == ("0002",)
    assert result.backup is not None
    assert result.backup.parent == settings.data_dir / "backups" / "pre-migration"
    assert "0001" in result.backup.name
    assert "0002" in result.backup.name
    assert current_revision(settings.database_path) == "0002"
    # The copy holds the schema as it was before the migration.
    assert current_revision(result.backup) == "0001"
    with closing(sqlite3.connect(result.backup)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    assert "extra" not in tables


def stamp_future_revision(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(settings.database_path)) as db:
        db.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        db.execute("INSERT INTO alembic_version VALUES ('from_the_future')")
        db.commit()


def test_refuses_to_start_on_newer_schema(settings: Settings) -> None:
    stamp_future_revision(settings)
    with (
        pytest.raises(SchemaTooNewError, match="from_the_future"),
        TestClient(create_app(settings)),
    ):
        pass


def test_migrate_refuses_a_newer_schema(settings: Settings) -> None:
    stamp_future_revision(settings)
    with pytest.raises(SchemaTooNewError):
        migrate(settings)
    assert not (settings.data_dir / "backups").exists()
