"""`backup run|verify`: the integrated backup (EF-INT-015, ENF-OPS-001, EF-INT-027).

What a run does, in this order, and what each failure leaves:

1. The passphrase file and the destination are settled *before any byte is produced*: without
   `SHELFHOLD_BACKUP_PASSPHRASE_FILE` the run fails with `passphrase_missing`, and nothing, not
   even a temporary file, was written. A passphrase file inside `/data`, the backed-up tree, or
   the backup directory is refused (`passphrase_colocated`) unless `accept_colocated` is given;
   that case is logged and leaves a durable warning for `status`.
2. The maintenance gate is held for the whole run (a monthly `VACUUM` waits, and does not start
   while a backup holds the gate). `VACUUM INTO` needs twice the size of the database free on the
   destination's volume (`has_room_for_vacuum`). The copy is a consistent snapshot; it is the one
   clear thing a run makes, in a 0700 folder of the backup directory, read into memory at once
   and removed in every case. The keyring is read *after* the snapshot, so it holds every key the
   snapshot's rows can cite (rotation only adds keys, retirement refuses a key still in use).
3. The set is one tar (`archive`), encrypted whole with age v1 / scrypt, written to the backup
   directory under a temporary name and given its own only once complete (`store`).
4. The written file is read back and fully checked (`verify`). Only then is the retention applied
   (the 14 newest stay): the oldest archive is never removed for a backup that was not verified.
5. Every outcome is a `backup_run` row; a failure is `backup.failed` with a cause from a closed
   list, and a last good backup older than `backup_stale_hours` is `backup.stale`.

The backup directory is opened for writing by `store` alone (import-linter contract
`backup-dir-sole-writer`); this module is the one sanctioned user of `store`.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import sqlite3
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import cache
from pathlib import Path
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.config import CONFIG_FILE_NAME, Settings
from shelfhold.docgen.release_assets import DICTIONARY_FILE, SCHEMA_FILE, write_assets
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.secrets import diagnostics
from shelfhold.infrastructure.secrets.errors import ExportError, KeyringError
from shelfhold.infrastructure.secrets.export import encrypt_export, read_passphrase_file
from shelfhold.infrastructure.volumes import read_data_file
from shelfhold.jobs.maintenance import MaintenanceBusyError
from shelfhold.ops.backup.archive import (
    DATABASE_MEMBER,
    KEYRING_MEMBER,
    MAX_ARCHIVE_BYTES,
    BackupArchiveError,
    encrypt_archive,
    pack,
)
from shelfhold.ops.backup.store import BackupDirectory, DirectoryError, archive_name
from shelfhold.ops.backup.verify import ARCHIVE_FILE_LIMIT, Checked, check_archive

__all__ = [
    "BACKUP_FAILURES",
    "BackupError",
    "BackupReport",
    "BackupService",
    "DiskSpace",
    "StaleReport",
    "backup_warnings",
    "destination_of",
    "passphrase_path_of",
    "verify_latest",
]

log = structlog.get_logger(__name__)

# The closed list of causes of `backup.failed` (the event carries one of these, nothing else).
BACKUP_FAILURES: Final = frozenset(
    {
        "passphrase_missing",
        "passphrase_invalid",
        "passphrase_colocated",
        "destination_missing",
        "destination_unavailable",
        "destination_not_distinct",
        "no_room",
        "too_large",
        "busy",
        "keyring_unavailable",
        "database_error",
        "write_failed",
        "verify_failed",
    }
)
COLOCATED_KEY: Final = "backup.colocated_passphrase"
SAME_DEVICE_KEY: Final = "backup.same_device"
STALE_KEY: Final = "backup.stale_announced"
GATE_WAIT_S: Final = 600.0
MAX_META_BYTES: Final = 256 * 1024 * 1024
MAX_FILE_BYTES: Final = 64 * 1024 * 1024
MAX_FILES: Final = 200_000
MANIFEST_FOLDER: Final = Path(".shelfhold") / "manifest"
_setting = platform.app_setting
_run = platform.backup_run


class BackupError(Exception):
    """A run that did not produce a verified backup; `reason` is in `BACKUP_FAILURES`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        assert reason in BACKUP_FAILURES  # noqa: S101 (a programming error, not input)
        self.reason = reason


class SpaceCheck(Protocol):
    def has_room_for_vacuum(self, *, database_bytes: int, volume: str) -> bool: ...


class Gate(Protocol):
    def hold(
        self, name: str, *, wait_s: float = 0.0
    ) -> contextlib.AbstractContextManager[None]: ...


class DiskSpace:
    """`has_room_for_vacuum` for the volume that takes the copy: twice the database free."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def has_room_for_vacuum(self, *, database_bytes: int, volume: str) -> bool:
        usage = os.statvfs(self._path)
        return usage.f_bavail * usage.f_frsize >= 2 * database_bytes


@dataclass(frozen=True)
class BackupReport:
    name: str
    size: int
    sha256: str
    members: int
    pruned: tuple[str, ...]
    schema_revision: str


@dataclass(frozen=True)
class StaleReport:
    last_ok: datetime | None
    stale: bool


@cache
def _assets() -> tuple[bytes, bytes]:
    """`schema.sql` and the data dictionary, generated from the migrations like the release's."""
    import tempfile

    with tempfile.TemporaryDirectory() as folder:
        schema, dictionary = write_assets(Path(folder))
        return schema.read_bytes(), dictionary.read_bytes()


def _inside(child: Path, parent: Path) -> bool:
    return child == parent or parent in child.parents


class BackupService:
    def __init__(
        self,
        settings: Settings,
        engine: Engine,
        *,
        clock: Clock,
        bus: EventBus,
        gate: Gate,
        space: SpaceCheck | None = None,
        assets: Callable[[], tuple[bytes, bytes]] = _assets,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._clock = clock
        self._bus = bus
        self._gate = gate
        self._space = space
        self._assets = assets

    # -- configuration ---------------------------------------------------------------------

    def configured(self) -> bool:
        """Some backup setting is given: a job that finds none stays silent (a fresh install)."""
        return self._settings.backup_dir is not None or (
            self._settings.backup_passphrase_file is not None
        )

    def _tree(self, include_manifests: bool) -> list[Path]:
        """The trees a backup copies: a passphrase file inside one defeats the encryption."""
        trees = [self._settings.data_dir.resolve()]
        if include_manifests:
            trees += [root / MANIFEST_FOLDER for root in self._roots()]
        return trees

    def _roots(self) -> list[Path]:
        with self._engine.connect() as connection:
            rows = connection.execute(sa.select(platform.physical_root.c.path_entry)).scalars()
            return [Path(p).resolve() for p in rows]

    def _passphrase(self, path: Path, destination: Path, *, accept_colocated: bool) -> str:
        """The passphrase, unless its file lies in a tree the backup copies or beside the
        archives (then only with `accept_colocated`, which is logged and flagged for `status`)."""
        try:
            passphrase = read_passphrase_file(path)
        except ExportError:
            raise BackupError("passphrase_invalid") from None
        register_secret_value(passphrase)
        trees = [*self._tree(include_manifests=True), destination]
        colocated = any(_inside(path.resolve(), tree) for tree in trees)
        if colocated and not accept_colocated:
            raise BackupError("passphrase_colocated")
        self._flag(COLOCATED_KEY, colocated)
        if colocated:
            log.warning("backup.colocated_passphrase_accepted")
        return passphrase

    # -- records ---------------------------------------------------------------------------

    def _flag(self, key: str, on: bool) -> None:
        stamp = self._clock.now().isoformat()
        with self._engine.begin() as connection:
            if on:
                connection.execute(
                    sqlite_insert(_setting)
                    .values(key=key, value=stamp)
                    .on_conflict_do_update(index_elements=["key"], set_={"value": stamp})
                )
            else:
                connection.execute(sa.delete(_setting).where(_setting.c.key == key))

    def _begin(self) -> str:
        run_id = new_ulid()
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(_run).values(id=run_id, started_at=self._clock.now(), state="running")
            )
        return run_id

    def _finish(self, run_id: str, **values: Any) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(_run)
                .where(_run.c.id == run_id)
                .values(finished_at=self._clock.now(), **values)
            )

    def _announce(self, event: str, payload: Mapping[str, object]) -> None:
        with self._engine.begin() as connection:
            published = self._bus.publish(connection, event, payload)
        self._bus.deliver([published])

    # -- the run ---------------------------------------------------------------------------

    def run(self, *, accept_colocated: bool = False, manifests: bool = False) -> BackupReport:
        """One backup. `manifests`: also copy the provenance mirror folders of the library roots;
        a scheduled run leaves them out (it must not wake a library disk outside the window,
        ENF-OPS-014), and they are projections of the database that `meta rebuild` writes again."""
        run_id = self._begin()
        try:
            report = self._run(run_id, accept_colocated=accept_colocated, manifests=manifests)
        except BackupError as error:
            self._finish(run_id, state="failed", cause=error.reason)
            log.warning("backup.failed", reason=error.reason)
            self._announce("backup.failed", {"reason": error.reason})
            self.announce_stale_if_due()
            raise
        except Exception:
            self._finish(run_id, state="failed", cause="write_failed")
            self._announce("backup.failed", {"reason": "write_failed"})
            raise
        self._finish(
            run_id, state="ok", file_name=report.name, size=report.size, sha256=report.sha256,
            schema_revision=report.schema_revision,
        )  # fmt: skip
        with self._engine.begin() as connection:
            connection.execute(sa.delete(_setting).where(_setting.c.key == STALE_KEY))
        log.info("backup.done", name=report.name, size=report.size, pruned=len(report.pruned))
        return report

    def _run(self, run_id: str, *, accept_colocated: bool, manifests: bool) -> BackupReport:
        path = passphrase_path_of(self._settings)  # first: nothing is made without it
        destination = destination_of(self._settings)
        passphrase = self._passphrase(path, destination, accept_colocated=accept_colocated)
        try:
            directory = BackupDirectory.open(destination)
            directory.sweep_stale(self._clock.now().timestamp())
        except DirectoryError as error:
            raise BackupError(error.reason) from None
        except OSError:
            raise BackupError("write_failed") from None
        self._flag(SAME_DEVICE_KEY, directory.device() == os.stat(self._settings.data_dir).st_dev)
        try:
            with self._gate.hold("backup", wait_s=GATE_WAIT_S):
                members, revision = self._collect(directory, passphrase, manifests)
        except MaintenanceBusyError:
            raise BackupError("busy") from None
        now = self._clock.now()
        count = len(members)
        try:
            tar_bytes = pack(members, created_at=now, schema_revision=revision)
        except ValueError:  # a member name that ustar cannot hold
            raise BackupError("write_failed") from None
        del members
        if len(tar_bytes) >= MAX_ARCHIVE_BYTES:
            raise BackupError("too_large")
        blob = encrypt_archive(tar_bytes, passphrase)
        del tar_bytes
        name = archive_name(now.strftime("%Y%m%dT%H%M%SZ"))
        try:
            directory.write(name, blob)
        except OSError:
            raise BackupError("write_failed") from None
        if not self._verified(directory, name, passphrase, blob):
            with contextlib.suppress(OSError):
                directory.drop(name)  # a file that does not verify is no backup
            raise BackupError("verify_failed")
        pruned = directory.prune(self._settings.backup_retention, protect=name)
        return BackupReport(
            name, len(blob), hashlib.sha256(blob).hexdigest(), count, tuple(pruned), revision
        )

    def _verified(
        self, directory: BackupDirectory, name: str, passphrase: str, blob: bytes
    ) -> bool:
        try:
            on_disk = directory.read(name, ARCHIVE_FILE_LIMIT)
            if on_disk != blob:
                return False
            check_archive(on_disk, passphrase)
        except (OSError, BackupArchiveError):
            return False
        return True

    def _database_bytes(self) -> int:
        database = self._settings.database_path
        return sum(p.stat().st_size for p in (database, Path(f"{database}-wal")) if p.exists())

    def _collect(
        self, directory: BackupDirectory, passphrase: str, manifests: bool
    ) -> tuple[dict[str, bytes], str]:
        space = self._space or DiskSpace(directory.path)
        if not space.has_room_for_vacuum(database_bytes=self._database_bytes(), volume="backup"):
            raise BackupError("no_room")
        members: dict[str, bytes] = {}
        with directory.workspace() as work:
            copy = work / "snapshot.db"
            revision = self._snapshot(copy)
            members[DATABASE_MEMBER] = _read_file(copy, MAX_ARCHIVE_BYTES)
        try:
            keyring = diagnostics.store_for(self._settings).load()
        except (KeyringError, OSError):
            raise BackupError("keyring_unavailable") from None
        members[KEYRING_MEMBER] = encrypt_export(keyring, passphrase).encode("ascii")
        data = self._settings.data_dir
        with contextlib.suppress(FileNotFoundError):
            members[CONFIG_FILE_NAME] = read_data_file(data / CONFIG_FILE_NAME, MAX_FILE_BYTES)
        members |= _tree_members(data / "meta", "meta")
        if manifests:
            for index, root in enumerate(self._roots()):
                members |= _tree_members(root / MANIFEST_FOLDER, f"manifests/{index:03d}")
        schema, dictionary = self._assets()
        members[f"docs/{SCHEMA_FILE}"], members[f"docs/{DICTIONARY_FILE}"] = schema, dictionary
        return members, revision

    def _snapshot(self, copy: Path) -> str:
        """`VACUUM INTO` a private copy; refuse it unless `integrity_check` says `ok`."""
        try:
            source = sqlite3.connect(self._settings.database_path, timeout=30, isolation_level=None)
            try:
                source.execute("VACUUM INTO ?", (str(copy),))
            finally:
                source.close()
            os.chmod(copy, 0o600)
            snapshot = sqlite3.connect(f"{copy.as_uri()}?mode=ro", uri=True)
            try:
                if snapshot.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise BackupError("database_error")
                row = snapshot.execute("SELECT version_num FROM alembic_version").fetchone()
            finally:
                snapshot.close()
        except sqlite3.Error:
            raise BackupError("database_error") from None
        if row is None:
            raise BackupError("database_error")
        return str(row[0])

    # -- verify, stale, status ---------------------------------------------------------------

    def last_ok(self) -> datetime | None:
        with self._engine.connect() as connection:
            return connection.execute(
                sa.select(sa.func.max(_run.c.finished_at)).where(_run.c.state == "ok")
            ).scalar()

    def stale(self) -> StaleReport:
        last = self.last_ok()
        limit = timedelta(hours=self._settings.backup_stale_hours)
        late = last is None or self._clock.now() - last > limit
        return StaleReport(last, self.configured() and late)

    def announce_stale_if_due(self) -> bool:
        """`backup.stale` once per episode: the marker is cleared by the next good backup."""
        report = self.stale()
        if not report.stale:
            return False
        with self._engine.begin() as connection:
            done = connection.execute(
                sqlite_insert(_setting)
                .values(key=STALE_KEY, value=self._clock.now().isoformat())
                .on_conflict_do_nothing()
            ).rowcount
        if not done:
            return False
        hours = self._settings.backup_stale_hours
        self._announce("backup.stale", {"hours": hours})
        return True


def passphrase_path_of(settings: Settings) -> Path:
    path = settings.backup_passphrase_file
    if path is None:
        raise BackupError("passphrase_missing")
    return path


def destination_of(settings: Settings) -> Path:
    """D2: a folder outside `/data` and `/store`, and not around them either."""
    if settings.backup_dir is None:
        raise BackupError("destination_missing")
    target = settings.backup_dir.resolve()
    for forbidden in (settings.data_dir.resolve(), settings.store_dir.resolve()):
        if _inside(target, forbidden) or _inside(forbidden, target):
            raise BackupError("destination_not_distinct")
    return target


def verify_latest(
    settings: Settings, *, passphrase_file: Path | None = None, name: str | None = None
) -> tuple[str, Checked]:
    """The newest archive (or `name`) of the backup directory, decrypted and checked. It needs no
    database and writes nothing, so it also runs on a host where only the backups survive."""
    path = passphrase_file or passphrase_path_of(settings)
    destination = destination_of(settings)
    try:
        passphrase = read_passphrase_file(path)
        directory = BackupDirectory.open_existing(destination)
    except ExportError:
        raise BackupError("passphrase_invalid") from None
    except DirectoryError as error:
        raise BackupError(error.reason) from None
    names = directory.names()
    chosen = name or (names[-1] if names else None)
    if chosen is None or chosen not in names:
        raise BackupArchiveError
    try:
        return chosen, check_archive(directory.read(chosen, ARCHIVE_FILE_LIMIT), passphrase)
    except OSError:
        raise BackupArchiveError from None


def _read_file(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(descriptor, "rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise BackupError("too_large")
    return data


def _tree_members(folder: Path, prefix: str) -> dict[str, bytes]:
    """The regular files under `folder` (links and special files are skipped, never followed),
    within a count and a size bound; a folder that is not there gives nothing."""
    found: dict[str, bytes] = {}
    total = 0
    try:
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            return found
    except OSError:
        return found
    pending = [folder]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda e: e.name)
        except OSError:
            continue
        for entry in entries:
            if entry.is_symlink():
                continue
            if entry.is_dir(follow_symlinks=False):
                pending.append(Path(entry.path))
            elif entry.is_file(follow_symlinks=False):
                data = read_data_file(Path(entry.path), MAX_FILE_BYTES + 1)
                total += len(data)
                if len(data) > MAX_FILE_BYTES or total > MAX_META_BYTES or len(found) >= MAX_FILES:
                    raise BackupError("too_large")
                rel = Path(entry.path).relative_to(folder).as_posix()
                found[f"{prefix}/{rel}"] = data
    return found


def backup_warnings(engine: Engine, settings: Settings, now: datetime) -> list[dict[str, str]]:
    """What `status` says about the backup: durable warnings, read from the database alone."""
    out: list[dict[str, str]] = []
    with engine.connect() as connection:
        marks = dict(
            connection.execute(
                sa.select(_setting.c.key, _setting.c.value).where(
                    _setting.c.key.in_([COLOCATED_KEY, SAME_DEVICE_KEY])
                )
            ).all()
        )
        last = connection.execute(
            sa.select(sa.func.max(_run.c.finished_at)).where(_run.c.state == "ok")
        ).scalar()
    configured = settings.backup_dir is not None or settings.backup_passphrase_file is not None
    if not configured:
        out.append({"code": "backup_not_configured", "detail": ""})
        return out
    if settings.backup_dir is None or settings.backup_passphrase_file is None:
        out.append({"code": "backup_incomplete", "detail": ""})
    if COLOCATED_KEY in marks:
        out.append({"code": "backup_passphrase_colocated", "detail": marks[COLOCATED_KEY]})
    if SAME_DEVICE_KEY in marks:
        out.append({"code": "backup_same_device", "detail": marks[SAME_DEVICE_KEY]})
    limit = timedelta(hours=settings.backup_stale_hours)
    if last is None or now - last > limit:
        out.append({"code": "backup_stale", "detail": "" if last is None else last.isoformat()})
    return out
