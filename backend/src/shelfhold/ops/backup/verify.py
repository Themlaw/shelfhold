"""Checking an archive without restoring anything (`backup verify`, and the read-back of a run).

Decrypt as a stream, parse, match every member to its digest as it passes, open the keyring
member with the same passphrase, and run `PRAGMA integrity_check` on the database. Memory does
not grow with the archive: the members are hashed and dropped, the database is spooled to a
private folder of `/data/tmp` (never to the backup destination; see `scratch`) and read by
SQLite from there, and removed. Any failure is the same generic `BackupArchiveError`.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from shelfhold.infrastructure.secrets.errors import ExportError, KeyringError
from shelfhold.infrastructure.secrets.export import decrypt_export
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.ops.backup.archive import (
    DATABASE_MEMBER,
    KEYRING_MEMBER,
    BackupArchiveError,
    TarParser,
    read_archive,
)
from shelfhold.ops.backup.scratch import scratch_folder

__all__ = ["Checked", "check_file", "check_stream", "open_archive_file"]

MAX_KEYRING_BYTES = 1024 * 1024


@dataclass(frozen=True)
class Checked:
    members: Mapping[str, int]
    keyring: Keyring
    created_at: str
    schema_revision: str
    file_sha256: str

    @property
    def member_count(self) -> int:
        return len(self.members)


def open_archive_file(path: Path) -> BinaryIO:
    """A regular file (not a link) opened for reading; what it holds is untrusted."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise BackupArchiveError from None
    stream = os.fdopen(descriptor, "rb")
    try:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise BackupArchiveError
    except BaseException:
        stream.close()
        raise
    return stream


class _HashingReader:
    """The archive file, hashed as it is read (the digest of the file `backup_run` records)."""

    def __init__(self, stream: BinaryIO) -> None:
        self._stream = stream
        self.digest = hashlib.sha256()

    def readline(self, size: int = -1, /) -> bytes:
        data = self._stream.readline(size)
        self.digest.update(data)
        return data

    def read(self, size: int = -1, /) -> bytes:
        data = self._stream.read(size)
        self.digest.update(data)
        return data

    def drain(self) -> None:
        while self.read(1 << 20):
            pass


class _Checker:
    """Keeps the keyring member in memory (it is small) and spools the database to a file."""

    def __init__(self, spool: Path | None) -> None:
        self._spool = spool
        self._keyring = bytearray()
        self._out: BinaryIO | None = None
        self._name = ""
        self.keyring_text = b""

    def begin(self, name: str, size: int) -> None:
        self._name = name
        if name == KEYRING_MEMBER:
            if size > MAX_KEYRING_BYTES:
                raise BackupArchiveError
            self._keyring = bytearray()
        elif name == DATABASE_MEMBER and self._spool is not None:
            descriptor = os.open(
                self._spool,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            self._out = os.fdopen(descriptor, "wb")

    def data(self, chunk: bytes) -> None:
        if self._name == KEYRING_MEMBER:
            self._keyring += chunk
        elif self._out is not None:
            self._out.write(chunk)

    def end(self, name: str) -> None:
        if name == KEYRING_MEMBER:
            self.keyring_text = bytes(self._keyring)
        if self._out is not None:
            self._out.close()
            self._out = None

    def close(self) -> None:
        if self._out is not None:
            self._out.close()


def _integrity_ok(database: Path) -> bool:
    try:
        connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        return connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def check_stream(
    stream: BinaryIO, passphrase: str, data_dir: Path | None, *, database: bool = True
) -> Checked:
    """`database=False` skips the integrity check of the database (the restore checks the file it
    wrote instead), which also spares the spool."""
    reader = _HashingReader(stream)
    if database:
        with scratch_folder(data_dir) as folder:
            checker = _Checker(folder / "check.db")
            try:
                parsed = read_archive(reader, passphrase, TarParser(checker))
            finally:
                checker.close()
            if not _integrity_ok(folder / "check.db"):
                raise BackupArchiveError
    else:
        checker = _Checker(None)
        parsed = read_archive(reader, passphrase, TarParser(checker))
    reader.drain()  # anything after the age file would be unhashed; the digest is of the file
    if not checker.keyring_text:
        raise BackupArchiveError
    try:
        keyring = decrypt_export(checker.keyring_text.decode("ascii"), passphrase)
    except (ExportError, KeyringError, UnicodeDecodeError):
        raise BackupArchiveError from None
    return Checked(
        parsed.members,
        keyring,
        parsed.created_at,
        parsed.schema_revision,
        reader.digest.hexdigest(),
    )


def check_file(path: Path, passphrase: str, data_dir: Path | None) -> Checked:
    with open_archive_file(path) as stream:
        try:
            return check_stream(stream, passphrase, data_dir)
        except OSError:
            raise BackupArchiveError from None
