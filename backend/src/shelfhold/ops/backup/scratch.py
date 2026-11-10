"""The private working folders of a backup, and the lock between two backups (EF-INT-015).

A run makes one clear thing: the `VACUUM INTO` snapshot of the database; a `verify` of an
archive spools the database it finds inside to check it. Neither may ever touch the backup
destination (a folder that rsync or restic copies off the host, on a file system whose modes may
mean nothing: FAT, CIFS, NFS). Both live in `/data/tmp/backup-<8 chars>/` instead: the same file
system as the database (which is in clear there anyway), mode 0700, one folder per use, locked
for as long as its owner is alive.

Each carries a marker file, written first; a folder without it is not ours and is left alone.
A kill leaves such a folder behind. `sweep_orphans` removes every `backup-*` folder that nobody
locks: at start-up and at the beginning of each run. Only plain files in a folder of that exact
name pattern are unlinked; anything else is left alone.

`BackupLock` is the exclusive lock of one backup against another (cron against a manual run, two
hosts of a shared volume are not a case: `/data` belongs to one instance).
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Final

__all__ = [
    "BackupLock",
    "LockBusyError",
    "scratch_folder",
    "stage_dir",
    "stage_file",
    "sweep_orphans",
]

SCRATCH_PARENT: Final = "tmp"
_NAME: Final = re.compile(r"backup-[A-Za-z0-9_]{8}")
LOCK_FILE: Final = ".backup.lock"
# Written first in every scratch folder this module makes: only a folder that carries it, with
# the exact name pattern, is ever removed (a folder of that name made by someone else is not ours).
MARKER: Final = ".shelfhold-backup-scratch"


class LockBusyError(Exception):
    """Another backup holds the lock."""


class BackupLock:
    """`flock` EX on `/data/.backup.lock`: held for a whole run."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / LOCK_FILE
        self._fd: int | None = None

    def __enter__(self) -> BackupLock:
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            if error.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                raise LockBusyError from None
            raise
        self._fd = descriptor
        return self

    def __exit__(self, *exc: object) -> None:
        if self._fd is not None:
            os.close(self._fd)  # closing releases the flock
            self._fd = None


def _parent(data_dir: Path) -> Path:
    parent = data_dir / SCRATCH_PARENT
    parent.mkdir(mode=0o700, exist_ok=True)
    return parent


@contextmanager
def scratch_folder(data_dir: Path | None) -> Iterator[Path]:
    """A 0700 folder under `/data/tmp`, locked while in use and removed in every case. Without a
    `/data` (a `verify` on a host where only the backups survived) it is a private system temp
    folder instead."""
    if data_dir is not None and data_dir.is_dir():
        folder = Path(tempfile.mkdtemp(prefix="backup-", dir=_parent(data_dir)))
    else:
        folder = Path(tempfile.mkdtemp(prefix="backup-"))
    descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fchmod(descriptor, 0o700)
        stage_file(folder, MARKER, b"shelfhold backup scratch\n")
        with contextlib.suppress(OSError):
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield folder
    finally:
        try:
            _remove_scratch(folder)
        finally:
            os.close(descriptor)


def _remove_scratch(folder: Path) -> None:
    """Only a `backup-xxxxxxxx` folder, and what the run put in it: plain files, links (the link
    itself, never its target) and the folders of the staged copies."""
    if _NAME.fullmatch(folder.name) is None or not _marked(folder):
        raise ValueError("not a scratch folder of the backup")
    folders = [folder]
    found: list[Path] = []  # every folder, parents first
    with contextlib.suppress(FileNotFoundError):
        while folders:
            current = folders.pop()
            found.append(current)
            with os.scandir(current) as entries:
                for entry in list(entries):
                    if entry.is_dir(follow_symlinks=False):
                        folders.append(Path(entry.path))
                    else:
                        os.unlink(entry.path)
        for current in reversed(found):
            os.rmdir(current)


def _marked(folder: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(folder / MARKER).st_mode)
    except OSError:
        return False


def stage_dir(parent: Path, name: str) -> Path:
    """A private (0700) sub-folder of a scratch folder."""
    path = parent / name
    path.mkdir(mode=0o700)
    return path


def stage_file(folder: Path, name: str, data: bytes) -> Path:
    """A private (0600) file in a scratch folder: the copy a member is read from."""
    path = folder / name
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    with os.fdopen(descriptor, "wb") as out:
        out.write(data)
    return path


def sweep_orphans(data_dir: Path) -> int:
    """Remove the scratch folders of runs that died: those nobody holds a lock on. The number
    removed."""
    parent = data_dir / SCRATCH_PARENT
    try:
        entries = [e for e in os.scandir(parent) if _NAME.fullmatch(e.name)]
    except OSError:
        return 0
    removed = 0
    for entry in entries:
        try:
            if not stat.S_ISDIR(os.lstat(entry.path).st_mode) or not _marked(Path(entry.path)):
                continue
            descriptor = os.open(entry.path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # its owner is alive
            _remove_scratch(Path(entry.path))
            removed += 1
        finally:
            os.close(descriptor)
    return removed
