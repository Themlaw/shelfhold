"""The backup directory: the single module that opens it for writing (EF-INT-027, D2).

The directory is a mounted folder outside `/data` and `/store` (the caller checks that), and it
holds encrypted files only: the archives `shelfhold-backup-<UTC>.tar.age` and the
`.partial-<16 hex>` file an archive is written to before it takes its name. Nothing in clear ever
reaches it (the snapshot of the database lives in `/data/tmp`, see `scratch`). What is deleted
here is one of those two kinds, matched by an exact name pattern, a regular file (never a link
or a folder), that starts with the archive's own first line, and, for an archive, that a
`backup_run` row of this instance recorded as written and verified: a file or a folder that
someone else put there never reaches an `unlink`. That is why the deleting function is classed
in the register of emitters (`backup_directory`).

An archive is placed under its name with `renameat2(RENAME_NOREPLACE)`; on a file system that
does not know it (FAT, exFAT, CIFS, some NFS: EPERM, EINVAL, ENOSYS) the placement is an existence
check followed by a rename, safe because the caller holds the exclusive backup lock.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import re
import secrets
import stat
from collections.abc import Callable, Collection
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Final

from shelfhold.infrastructure.fsops.openat2 import renameat2_noreplace
from shelfhold.infrastructure.secrets.export import ByteSink
from shelfhold.ops.backup.archive import ARCHIVE_MAGIC

__all__ = ["ARCHIVE_NAME", "BackupDirectory", "DirectoryError", "archive_name"]

ARCHIVE_NAME: Final = re.compile(r"shelfhold-backup-\d{8}T\d{6}Z\.tar\.age")
_PARTIAL: Final = re.compile(r"\.partial-[0-9a-f]{16}")
STALE_AFTER_S: Final = 3600
_NO_RENAME2: Final = (errno.EPERM, errno.EINVAL, errno.ENOSYS, errno.ENOTSUP)


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

    def open_archive(self, name: str) -> BinaryIO:
        """An archive for reading: never a link, only a regular file."""
        descriptor = os.open(
            self._file(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
        )
        stream = os.fdopen(descriptor, "rb")
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            stream.close()
            raise OSError("not a regular file")
        return stream

    def write(self, name: str, fill: Callable[[ByteSink], None]) -> tuple[int, str]:
        """The archive appears under its name whole or not at all, mode 0600, and never replaces
        an archive (`FileExistsError`). `fill` writes it to the sink it is given, in chunks.
        Whatever fails leaves no partial file behind. Returns the size and the sha256 written."""
        final = self._file(name)
        partial_name = f".partial-{secrets.token_hex(8)}"
        partial = self._file(partial_name)
        descriptor = os.open(
            partial, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600
        )
        sink = _Sink(descriptor)
        try:
            try:
                os.fchmod(descriptor, 0o600)
                fill(sink)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._place(partial, final)
        except BaseException:
            self._discard(partial_name)
            raise
        self._discard(partial_name)  # the link-less fallbacks leave nothing; a no-op after rename
        self._sync()
        return sink.size, sink.digest.hexdigest()

    @staticmethod
    def _place(partial: Path, final: Path) -> None:
        folder = os.open(partial.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            renameat2_noreplace(folder, partial.name, folder, final.name)
        except OSError as error:
            if error.errno not in _NO_RENAME2:
                raise
            # no renameat2 on this file system: look, then rename (the backup lock is held)
            try:
                os.lstat(final)
            except FileNotFoundError:
                os.rename(partial, final)
            else:
                raise FileExistsError(errno.EEXIST, "exists", str(final)) from None
        finally:
            os.close(folder)

    def _sync(self) -> None:
        descriptor = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _ours(self, name: str) -> bool:
        """A regular file (no link) that starts like one of our archives."""
        try:
            descriptor = os.open(
                self._file(name), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
            )
        except OSError:
            return False
        with os.fdopen(descriptor, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return False
            head = stream.read(len(ARCHIVE_MAGIC))
        # the whole first line's start, for both: an empty or short file is not ours to remove
        return head == ARCHIVE_MAGIC

    def prune(self, keep: int, protect: str, known: Collection[str]) -> list[str]:
        """Retention: among the archives this instance recorded (`known`: the files of its
        `backup_run` rows in state `ok`) and that still start like one, the oldest beyond `keep`
        leave, never `protect` (the one just written and verified). Called only after that
        verification. A file that is not recorded stays, whatever its name."""
        if keep < 1:
            raise ValueError("a retention below one backup would delete the last one")
        names = [n for n in self.names() if n in known and self._ours(n)]
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
            if not stat.S_ISREG(os.lstat(target).st_mode):
                return
            os.unlink(target)

    def sweep_partials(self, now_s: float) -> int:
        """A killed write leaves a `.partial-*` file (encrypted, so harmless but dead weight):
        one older than an hour that starts like ours is removed before the next run."""
        removed = 0
        with os.scandir(self.path) as entries:
            found = [e.name for e in entries if _PARTIAL.fullmatch(e.name)]
        for name in found:
            try:
                age = now_s - os.lstat(self.path / name).st_mtime
            except OSError:
                continue
            if age >= STALE_AFTER_S and self._ours(name):
                self._discard(name)
                removed += 1
        return removed


class _Sink:
    """The write end of the partial file: counts and hashes what it writes."""

    def __init__(self, descriptor: int) -> None:
        self._fd = descriptor
        self.size = 0
        self.digest = hashlib.sha256()

    def write(self, data: bytes, /) -> int:
        view = memoryview(data)
        while view:
            view = view[os.write(self._fd, view) :]
        self.size += len(data)
        self.digest.update(data)
        return len(data)
