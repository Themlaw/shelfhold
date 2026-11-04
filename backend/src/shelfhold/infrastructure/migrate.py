"""`shelfhold migrate` (ENF-OPS-002): backup, forward-only Alembic upgrade, newer-schema guard."""

import contextlib
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import structlog
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError

from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.volumes import ensure_volumes

STATE_FILE_NAME = "migration-state.json"


class SchemaTooNewError(Exception):
    """The database was written by a more recent Shelfhold: refuse to run (never downgrade)."""

    def __init__(self, revision: str) -> None:
        super().__init__(f"database schema revision {revision!r} is newer than this release")
        self.revision = revision


class MigrationError(Exception):
    def __init__(self, revision: str, detail: str) -> None:
        super().__init__(f"migration {revision} failed: {detail}")
        self.revision = revision
        self.detail = detail


@dataclass(frozen=True)
class MigrationResult:
    from_revision: str | None
    to_revision: str
    applied: tuple[str, ...]
    backup: Path | None


class MigrationStateFile:
    """Progress shared with `/healthz` while a migration runs (`state: migrating`)."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / STATE_FILE_NAME

    def write(self, *, done: int, total: int, pid: int | None = None) -> None:
        payload = {"pid": pid if pid is not None else os.getpid(), "done": done, "total": total}
        temporary = self._path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        temporary.replace(self._path)

    def read(self) -> tuple[int, int] | None:
        """Progress `(done, total)`, or None when no live process is migrating."""
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            done, total, pid = int(payload["done"]), int(payload["total"]), int(payload["pid"])
        except (OSError, ValueError, KeyError):
            return None
        return (done, total) if _process_alive(pid) else None  # a crashed run leaves no trace

    def clear(self) -> None:
        self._path.unlink(missing_ok=True)


def _process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def default_script_location() -> Path:
    return Path(__file__).parent / "migrations"


def _script(script_location: Path | None) -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(script_location or default_script_location()))
    return ScriptDirectory.from_config(config)


def head_revision(script_location: Path | None = None) -> str:
    head = _script(script_location).get_current_head()
    if head is None:
        raise RuntimeError("no migration revision found")
    return head


def current_revision(database_path: Path) -> str | None:
    """Revision recorded in the database, None when it is absent or never migrated."""
    if not database_path.exists():
        return None
    connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
    try:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()
    return str(row[0]) if row else None


def check_schema_not_newer(settings: Settings, script_location: Path | None = None) -> None:
    current = current_revision(settings.database_path)
    if current is None:
        return
    try:
        _script(script_location).get_revision(current)
    except CommandError as exc:
        structlog.get_logger().error("schema_too_new", revision=current)
        raise SchemaTooNewError(current) from exc


def pre_migration_backup_path(settings: Settings, from_revision: str, to_revision: str) -> Path:
    """Named by revision and kept outside any rotation (ENF-OPS-002)."""
    directory = settings.data_dir / "backups" / "pre-migration"
    return directory / f"shelfhold-{from_revision}-to-{to_revision}.db"


def _backup_database(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.unlink(missing_ok=True)
    reader = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    writer = sqlite3.connect(destination)
    try:
        reader.backup(writer)  # consistent copy, WAL included
    finally:
        writer.close()
        reader.close()


def _steps_after(script: ScriptDirectory, current: str | None) -> list[str]:
    ordered = [rev.revision for rev in reversed(list(script.walk_revisions("base", "heads")))]
    return ordered[ordered.index(current) + 1 :] if current else ordered


def migrate(settings: Settings, *, script_location: Path | None = None) -> MigrationResult:
    log = structlog.get_logger()
    ensure_volumes(settings)
    check_schema_not_newer(settings, script_location)
    script = _script(script_location)
    head = head_revision(script_location)
    current = current_revision(settings.database_path)
    steps = _steps_after(script, current)
    if not steps:
        log.info("migration_up_to_date", revision=head)
        return MigrationResult(current, head, (), None)

    backup: Path | None = None
    if current is not None:  # a fresh database has nothing worth saving
        backup = pre_migration_backup_path(settings, current, head)
        _backup_database(settings.database_path, backup)
        log.info("pre_migration_backup", path=str(backup), revision=current)

    state = MigrationStateFile(settings.data_dir)
    state.write(done=0, total=len(steps))
    config = Config()
    config.set_main_option("script_location", str(script_location or default_script_location()))
    engine = create_db_engine(settings.database_path)
    try:
        for index, revision in enumerate(steps):
            log.info("migration_started", revision=revision, step=index + 1, total=len(steps))
            try:
                with engine.begin() as connection:
                    config.attributes["connection"] = connection
                    command.upgrade(config, revision)
            except Exception as exc:
                log.error(
                    "migration_failed",
                    revision=revision,
                    error=str(exc),
                    backup=str(backup) if backup else None,
                    hint="restore the backup, fix the cause, then run shelfhold migrate again",
                )
                raise MigrationError(revision, str(exc)) from exc
            state.write(done=index + 1, total=len(steps))
    finally:
        engine.dispose()
        with contextlib.suppress(OSError):
            state.clear()
    log.info("migration_done", revision=head, applied=len(steps))
    return MigrationResult(current, head, tuple(steps), backup)
