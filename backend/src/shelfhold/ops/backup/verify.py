"""Checking an archive without restoring anything (`backup verify`, and the read-back of a run).

Decrypt, parse, match every member to its digest, open the keyring member with the same
passphrase, and run `PRAGMA integrity_check` on the database *in memory* (`deserialize`): no
clear byte is written for the check. Any failure is the same generic `BackupArchiveError`.
"""

from __future__ import annotations

import os
import sqlite3
import stat
from dataclasses import dataclass
from pathlib import Path

from shelfhold.infrastructure.secrets.errors import ExportError, KeyringError
from shelfhold.infrastructure.secrets.export import decrypt_export
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.ops.backup.archive import (
    DATABASE_MEMBER,
    KEYRING_MEMBER,
    MAX_ARCHIVE_BYTES,
    BackupArchiveError,
    Members,
    decrypt_archive,
    unpack,
)

__all__ = ["Checked", "check_archive", "read_archive_file"]

ARCHIVE_FILE_LIMIT = MAX_ARCHIVE_BYTES + 1024 * 1024


@dataclass(frozen=True)
class Checked:
    members: Members
    keyring: Keyring
    created_at: str
    schema_revision: str

    @property
    def member_count(self) -> int:
        return len(self.members)


def read_archive_file(path: Path) -> bytes:
    """A regular file (not a link), read within a bound; what it holds is untrusted."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        raise BackupArchiveError from None
    with os.fdopen(descriptor, "rb") as stream:
        try:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise BackupArchiveError
            blob = stream.read(ARCHIVE_FILE_LIMIT + 1)
        except OSError:
            raise BackupArchiveError from None
    if len(blob) > ARCHIVE_FILE_LIMIT:
        raise BackupArchiveError
    return blob


def _integrity_ok(database: bytes) -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.deserialize(database)
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        return rows == [("ok",)]
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def check_archive(blob: bytes, passphrase: str) -> Checked:
    parsed = unpack(decrypt_archive(blob, passphrase))
    keyring_armored = parsed.members.get(KEYRING_MEMBER)
    if keyring_armored is None:
        raise BackupArchiveError
    try:
        keyring = decrypt_export(keyring_armored.decode("ascii"), passphrase)
    except (ExportError, KeyringError, UnicodeDecodeError):
        raise BackupArchiveError from None
    if not _integrity_ok(parsed.members[DATABASE_MEMBER]):
        raise BackupArchiveError
    return Checked(parsed.members, keyring, parsed.created_at, parsed.schema_revision)
