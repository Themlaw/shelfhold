"""`backup restore`: the files of an archive written under a new folder (EF-INT-015).

Nothing is written before the archive has been decrypted, parsed and every member checked
(`check_archive`), so a truncated, altered or forged archive leaves the destination untouched.
The destination is a folder that does not exist yet or is empty, never a link, and never the
folder of the running instance or one inside it ("a different path"). Each member is created with
`openat`-style calls from a descriptor on the destination, every folder on the way opened with
`O_NOFOLLOW`, every file with `O_EXCL | O_NOFOLLOW`: a name cannot climb out (the archive is
refused for a `..`, an absolute name or a link before this point), and a link planted in the
destination by someone else is not followed. Nothing is ever deleted, here or by the caller: a
restore that stops half way leaves its folder and says so.

The database is written last, so a half restore has no database and cannot be started by
mistake. The keyring is written as the file the instance reads (`keyring`, 0600) from the
armored member. What follows is the operator's: start Shelfhold on the new folder (it enters
`restored_behind` if a library root is ahead of the database), then
`shelfhold reconcile --adopt-after-restore`.
"""

from __future__ import annotations

import contextlib
import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from shelfhold.infrastructure.secrets.errors import ExportError
from shelfhold.infrastructure.secrets.export import read_passphrase_file
from shelfhold.ops.backup.archive import (
    DATABASE_MEMBER,
    KEYRING_MEMBER,
    BackupArchiveError,
    member_name_ok,
)
from shelfhold.ops.backup.verify import Checked, check_archive, read_archive_file

__all__ = ["DestinationError", "PassphraseError", "RestoreReport", "restore_archive"]

KEYRING_FILE = "keyring"


class DestinationError(Exception):
    """The destination cannot take a restore; `reason` is a short code for the command line."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class PassphraseError(Exception):
    """The passphrase file is refused (the message names the fix, never the passphrase)."""


@dataclass(frozen=True)
class RestoreReport:
    destination: Path
    files: int
    schema_revision: str
    created_at: str


def _check_destination(destination: Path, live_data_dir: Path | None) -> Path:
    resolved = destination.resolve()
    if live_data_dir is not None:
        live = live_data_dir.resolve()
        if resolved == live or live in resolved.parents:
            raise DestinationError("is_live_data")
    try:
        status = os.lstat(destination)
    except FileNotFoundError:
        status = None
    except OSError:
        raise DestinationError("unusable") from None
    if status is not None and (
        stat.S_ISLNK(status.st_mode) or not stat.S_ISDIR(status.st_mode) or os.listdir(destination)
    ):
        raise DestinationError("not_empty")
    if status is None:
        try:
            os.mkdir(destination, 0o700)
        except OSError:
            raise DestinationError("unusable") from None
    return resolved


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]
    os.fsync(descriptor)


def _write_member(root_fd: int, rel: str, data: bytes) -> None:
    if not member_name_ok(rel):  # the same rule as the archive's, on the way out as well
        raise BackupArchiveError
    parts = PurePosixPath(rel).parts
    folder = os.dup(root_fd)
    try:
        for part in parts[:-1]:
            with contextlib.suppress(FileExistsError):
                os.mkdir(part, 0o700, dir_fd=folder)
            inner = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=folder
            )
            os.close(folder)
            folder = inner
        out = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
            dir_fd=folder,
        )
        try:
            os.fchmod(out, 0o600)
            _write_all(out, data)
        finally:
            os.close(out)
        os.fsync(folder)
    finally:
        os.close(folder)


def restore_archive(
    archive: Path,
    destination: Path,
    passphrase_file: Path,
    *,
    live_data_dir: Path | None = None,
) -> RestoreReport:
    """Raises `PassphraseError`, `BackupArchiveError` (damaged, altered, wrong passphrase: one
    message) or `DestinationError`; in the first two cases nothing was written."""
    try:
        passphrase = read_passphrase_file(passphrase_file)
    except ExportError as error:
        raise PassphraseError(str(error)) from None
    checked = check_archive(read_archive_file(archive), passphrase)
    del passphrase
    resolved = _check_destination(destination, live_data_dir)
    root_fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        files = 0
        for rel, data in sorted(checked.members.items()):
            if rel in (DATABASE_MEMBER, KEYRING_MEMBER):
                continue
            _write_member(root_fd, rel, data)
            files += 1
        _write_keyring(root_fd, checked)
        _write_member(root_fd, DATABASE_MEMBER, checked.members[DATABASE_MEMBER])
    finally:
        os.close(root_fd)
    return RestoreReport(resolved, files + 2, checked.schema_revision, checked.created_at)


def _write_keyring(root_fd: int, checked: Checked) -> None:
    out = os.open(
        KEYRING_FILE,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
        dir_fd=root_fd,
    )
    try:
        os.fchmod(out, 0o600)
        _write_all(out, checked.keyring.to_json())
    finally:
        os.close(out)
