"""The backup directory: the single module that opens it for writing (EF-INT-027, D2).

The directory is a mounted folder outside `/data` and `/store` (the caller checks that). Three
kinds of entry live in it, and only these: the archives `shelfhold-backup-<UTC>.tar.age`, the
`.partial-*` file an archive is written to before it takes its name, and a `.work-*` folder
(mode 0700) that holds the one clear thing a backup ever makes, the `VACUUM INTO` copy of the
database, for as long as it takes to read it into memory. Everything deleted here is one of
those, matched by an exact pattern, a regular file or a folder this module made itself: a name
given by anyone else never reaches an `unlink`. That is why the deleting functions are classed
in the register of emitters (`backup_directory`).
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

__all__ = ["ARCHIVE_NAME", "BackupDirectory", "DirectoryError", "archive_name"]

ARCHIVE_NAME: Final = re.compile(r"shelfhold-backup-\d{8}T\d{6}Z\.tar\.age")
_PARTIAL: Final = re.compile(r"\.partial-[0-9a-f]{16}")
_WORK: Final = re.compile(r"\.work-[A-Za-z0-9_]{8}")
STALE_AFTER_S: Final = 3600


class DirectoryError(Exception):
    """The directory cannot be used; `reason` is a cause of `backup.failed`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def archive_name(stamp: str) -> str:
    """`stamp` is `YYYYMMDDTHHMMSSZ`."""
    name = f"shelfhold-backup-{stamp}.tar.age"
    if ARCHIVE_NAME.fullmatch(name) is None:
        raise ValueError("not a backup stamp")
    return name


@dataclass(frozen=True)
class BackupDirectory:
    path: Path

    @classmethod
    def open(cls, path: Path) -> BackupDirectory:
        """An existing folder (not a link), or one made here under an existing parent."""
        try:
            try:
                status = os.lstat(path)
            except FileNotFoundError:
                os.mkdir(path, 0o700)
                status = os.lstat(path)
        except OSError:
            raise DirectoryError("destination_unavailable") from None
        if not stat.S_ISDIR(status.st_mode):
            raise DirectoryError("destination_unavailable")
        if not os.access(path, os.W_OK | os.X_OK):
            raise DirectoryError("destination_unavailable")
        return cls(path)

    @classmethod
    def open_existing(cls, path: Path) -> BackupDirectory:
        """A folder that is already there (a read-only use: nothing is created)."""
        try:
            status = os.lstat(path)
        except OSError:
            raise DirectoryError("destination_unavailable") from None
        if not stat.S_ISDIR(status.st_mode):
            raise DirectoryError("destination_unavailable")
        return cls(path)

    def device(self) -> int:
        return os.stat(self.path).st_dev

    def _file(self, name: str) -> Path:
        if not (ARCHIVE_NAME.fullmatch(name) or _PARTIAL.fullmatch(name)):
            raise ValueError("not a name this directory holds")
        return self.path / name

    def names(self) -> list[str]:
        """The archives, oldest first (the stamp sorts as time). A link or a folder with such a
        name is not an archive."""
        found: list[str] = []
        with os.scandir(self.path) as entries:
            for entry in entries:
                if ARCHIVE_NAME.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                    found.append(entry.name)
        return sorted(found)

    def read(self, name: str, limit: int) -> bytes:
        """An archive's bytes; never a link, never more than `limit + 1` bytes."""
        descriptor = os.open(
            self._file(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        )
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("not a regular file")
            return stream.read(limit + 1)

    def write(self, name: str, data: bytes) -> None:
        """The archive appears under its name whole or not at all, mode 0600, and never replaces
        an archive (`FileExistsError`). Whatever fails leaves no partial file behind."""
        final = self._file(name)
        partial_name = f".partial-{secrets.token_hex(8)}"
        partial = self._file(partial_name)
        descriptor = os.open(
            partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        try:
            try:
                os.fchmod(descriptor, 0o600)
                view = memoryview(data)
                while view:
                    view = view[os.write(descriptor, view) :]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.link(partial, final)  # fails instead of replacing
        except BaseException:
            self._discard(partial_name)
            raise
        self._discard(partial_name)
        self._sync()

    def _sync(self) -> None:
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def prune(self, keep: int, protect: str) -> list[str]:
        """Retention: the oldest archives beyond `keep` leave, never `protect` (the one just
        written and verified). Called only after that verification."""
        if keep < 1:
            raise ValueError("a retention below one backup would delete the last one")
        names = self.names()
        excess = [n for n in names[: max(len(names) - keep, 0)] if n != protect]
        for name in excess:
            self._discard(name)
        return excess

    def drop(self, name: str) -> None:
        """Remove one archive this directory holds (a written file that did not verify)."""
        if ARCHIVE_NAME.fullmatch(name) is None:
            raise ValueError("not an archive name")
        self._discard(name)

    def _discard(self, name: str) -> None:
        """Remove one archive or partial file of this directory (exact pattern, not a folder)."""
        target = self._file(name)
        with contextlib.suppress(FileNotFoundError):
            if stat.S_ISDIR(os.lstat(target).st_mode):
                return
            os.unlink(target)

    @contextmanager
    def workspace(self) -> Iterator[Path]:
        """A private folder (0700) for the clear copy of the database, removed in every case:
        an exception, a kill of the code path, a normal exit."""
        folder = Path(tempfile.mkdtemp(prefix=".work-", dir=self.path))
        try:
            os.chmod(folder, 0o700)
            yield folder
        finally:
            self._remove_work_dir(folder)

    def _remove_work_dir(self, folder: Path) -> None:
        """Only a `.work-*` folder of this directory, and only the plain files in it."""
        if folder.parent != self.path or _WORK.fullmatch(folder.name) is None:
            raise ValueError("not a work folder of this directory")
        with contextlib.suppress(FileNotFoundError):
            with os.scandir(folder) as entries:
                for entry in entries:
                    if entry.is_file(follow_symlinks=False) or entry.is_symlink():
                        os.unlink(entry.path)
            os.rmdir(folder)

    def sweep_stale(self, now_s: float) -> int:
        """A killed run leaves a `.work-*` folder (a clear copy!) or a partial file: both older
        than an hour are removed before the next run."""
        removed = 0
        with os.scandir(self.path) as entries:
            found = [(e.name, e.path) for e in entries]
        for name, full in found:
            try:
                age = now_s - os.lstat(full).st_mtime
            except OSError:
                continue
            if age < STALE_AFTER_S:
                continue
            if _WORK.fullmatch(name):
                self._remove_work_dir(self.path / name)
                removed += 1
            elif _PARTIAL.fullmatch(name):
                self._discard(name)
                removed += 1
        return removed
