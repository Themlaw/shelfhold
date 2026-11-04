"""`shelfhold migrate` (ENF-OPS-002): backup, forward-only Alembic upgrade, newer-schema guard."""

import contextlib
import fcntl
import json
import os
import shutil
import sqlite3
import time
from collections.abc import Iterator
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
LOCK_FILE_NAME = "migrate.lock"
LOCK_GRACE_SECONDS = 1.0


class SchemaTooNewError(Exception):
    """The database was written by a more recent Shelfhold: refuse to run (never downgrade)."""

    def __init__(self, revision: str) -> None:
        super().__init__(f"database schema revision {revision!r} is newer than this release")
        self.revision = revision


class MigrationInProgressError(Exception):
    """Another `shelfhold migrate` holds the migration lock."""


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
    """Migration lock and progress, shared with `/healthz` (`state: migrating`).

    The migrator holds an exclusive `flock` on a file of `/data` for its whole run; the kernel
    releases it if the process dies, so a crashed run can never leave the state stuck. Readers
    only try a non-blocking lock: failing to get it means a migration is running.
    """

    def __init__(self, data_dir: Path) -> None:
        self._lock_path = data_dir / LOCK_FILE_NAME
        self._progress_path = data_dir / STATE_FILE_NAME

    @contextlib.contextmanager
    def exclusive(self) -> Iterator[None]:
        """Held by `migrate`; raises MigrationInProgressError if another run holds it."""
        descriptor = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + LOCK_GRACE_SECONDS  # a health probe may hold it briefly
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise MigrationInProgressError from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def write(self, *, done: int, total: int) -> None:
        temporary = self._progress_path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"done": done, "total": total}), encoding="utf-8")
        temporary.replace(self._progress_path)

    def read(self) -> tuple[int, int] | None:
        """Progress `(done, total)` while a migration holds the lock, else None."""
        try:
            descriptor = os.open(self._lock_path, os.O_RDONLY)
        except FileNotFoundError:
            return None
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
            except BlockingIOError:
                return self._progress()
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            return None
        finally:
            os.close(descriptor)

    def _progress(self) -> tuple[int, int]:
        try:
            payload = json.loads(self._progress_path.read_text(encoding="utf-8"))
            return int(payload["done"]), int(payload["total"])
        except (OSError, ValueError, KeyError):
            return 0, 0  # the lock is held but the first step is not announced yet

    def clear(self) -> None:
        self._progress_path.unlink(missing_ok=True)


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


def _read_only_uri(path: Path) -> str:
    return f"{path.resolve().as_uri()}?mode=ro"


def current_revision(database_path: Path) -> str | None:
    """Revision recorded in the database, None when it is absent or never migrated."""
    if not database_path.exists():
        return None
    connection = sqlite3.connect(_read_only_uri(database_path), uri=True)
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
    """Named by revision, outside any rotation, and never an existing file (ENF-OPS-002)."""
    directory = settings.data_dir / "backups" / "pre-migration"
    stem = f"shelfhold-{from_revision}-to-{to_revision}"
    candidate = directory / f"{stem}.db"
    attempt = 1
    while candidate.exists():
        candidate = directory / f"{stem}.{attempt}.db"
        attempt += 1
    return candidate


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _backup_database(source: Path, destination: Path) -> None:
    """Consistent copy (WAL included): temp file, fsync, atomic rename, mode 0600."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    os.close(os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600))
    reader = sqlite3.connect(_read_only_uri(source), uri=True)
    writer = sqlite3.connect(temporary)
    try:
        reader.backup(writer)
    finally:
        writer.close()
        reader.close()
    descriptor = os.open(temporary, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, destination)
    _fsync_directory(destination.parent)


def _restore_database(backup: Path | None, database: Path) -> None:
    """Put the database back as it was before the run (SQLite DDL is not rolled back)."""
    for suffix in ("-wal", "-shm"):
        Path(str(database) + suffix).unlink(missing_ok=True)
    if backup is None:
        database.unlink(missing_ok=True)  # the run created it
        return
    temporary = database.with_name(database.name + ".restore")
    with backup.open("rb") as source:
        descriptor = os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
        with os.fdopen(descriptor, "wb") as target:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
    os.chmod(temporary, 0o600)  # an existing file keeps its old mode otherwise
    os.replace(temporary, database)
    _fsync_directory(database.parent)


def _steps_after(script: ScriptDirectory, current: str | None) -> list[str]:
    ordered = [rev.revision for rev in reversed(list(script.walk_revisions("base", "heads")))]
    return ordered[ordered.index(current) + 1 :] if current else ordered


def migrate(settings: Settings, *, script_location: Path | None = None) -> MigrationResult:
    ensure_volumes(settings)
    state = MigrationStateFile(settings.data_dir)
    with state.exclusive():
        try:
            return _migrate_locked(settings, script_location, state)
        finally:
            with contextlib.suppress(OSError):
                state.clear()


def _migrate_locked(
    settings: Settings, script_location: Path | None, state: MigrationStateFile
) -> MigrationResult:
    log = structlog.get_logger()
    check_schema_not_newer(settings, script_location)
    script = _script(script_location)
    head = head_revision(script_location)
    current = current_revision(settings.database_path)
    steps = _steps_after(script, current)
    if not steps:
        log.info("migration_up_to_date", revision=head)
        return MigrationResult(current, head, (), None)

    backup: Path | None = None
    if settings.database_path.exists() and settings.database_path.stat().st_size > 0:
        backup = pre_migration_backup_path(settings, current or "base", head)
        _backup_database(settings.database_path, backup)
        log.info("pre_migration_backup", path=str(backup), revision=current)

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
                engine.dispose()
                _restore_database(backup, settings.database_path)
                log.error(
                    "migration_failed",
                    revision=revision,
                    error=str(exc),
                    backup=str(backup) if backup else None,
                )
                log.info("migration_restored", revision=current, from_backup=backup is not None)
                raise MigrationError(revision, str(exc)) from exc
            state.write(done=index + 1, total=len(steps))
    finally:
        engine.dispose()
    log.info("migration_done", revision=head, applied=len(steps))
    return MigrationResult(current, head, tuple(steps), backup)
