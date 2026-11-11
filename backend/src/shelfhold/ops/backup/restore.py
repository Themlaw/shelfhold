"""`backup restore`: the files of an archive written under a new folder (EF-INT-015).

Nothing is written before the archive has been decrypted, parsed and every member checked
(a first, streaming pass: `check_stream`), so a truncated, altered or forged archive leaves the
destination untouched; a second pass decrypts it again and writes each member as it arrives
(memory stays bounded whatever the size), checking every digest once more.
The destination is a folder that does not exist yet or is empty, never a link, and never the
folder of the running instance or one inside it ("a different path"). Each member is created with
`openat`-style calls from a descriptor on the destination, every folder on the way opened with
`O_NOFOLLOW`, every file with `O_EXCL | O_NOFOLLOW`: a name cannot climb out (the archive is
refused for a `..`, an absolute name or a link before this point), and a link planted in the
destination by someone else is not followed. Nothing is ever deleted, here or by the caller: a
restore that stops half way leaves its folder and says so.

The database is written last, so a half restore has no database and cannot be started by
mistake, and is integrity-checked where it lands. Before it takes its name, every session and
API key in it is revoked (a key revoked after the backup would be live again otherwise) and a
notice is left in the folder: `status` of the restored instance asks for the passkeys and
passwords changed since the backup to be checked, until `shelfhold restore acknowledge`. The
keyring is written as the file the
instance reads (`keyring`, 0600) from the armored member, unless the instance keeps its keyring
elsewhere (`SHELFHOLD_SECRET_KEY_FILE`): then that file is the operator's and nothing is
recreated in the new folder. What follows is the operator's: start Shelfhold on the new folder
(it enters
`restored_behind` if a library root is ahead of the database), then
`shelfhold reconcile --adopt-after-restore`.
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO

import sqlalchemy as sa

from shelfhold.identity.public import revoke_all_credentials
from shelfhold.infrastructure.identity_counter import COUNTER_KEY, IdentityCounter
from shelfhold.infrastructure.restore_notice import RestoreNoticeData
from shelfhold.infrastructure.secrets.errors import ExportError
from shelfhold.infrastructure.secrets.export import read_passphrase_file
from shelfhold.ops.backup.archive import (
    DATABASE_MEMBER,
    KEYRING_MEMBER,
    BackupArchiveError,
    TarParser,
    member_name_ok,
    read_archive,
)
from shelfhold.ops.backup.verify import HashingReader, check_stream, open_archive_file

__all__ = [
    "DestinationError",
    "PassphraseError",
    "RestoreIncompleteError",
    "RestoreReport",
    "restore_archive",
]

KEYRING_FILE = "keyring"
DATABASE_TEMP = "shelfhold.db.restoring"  # the database lands under this name, and is renamed last


class DestinationError(Exception):
    """The destination cannot take a restore; `reason` is a short code for the command line."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RestoreIncompleteError(Exception):
    """The archive was fine when it was checked, but not when it was written (it changed in
    between, or the restored database fails its check): the destination holds a part of it, and
    no `shelfhold.db`. Nothing is deleted; the operator empties the folder and starts again."""


class PassphraseError(Exception):
    """The passphrase file is refused (the message names the fix, never the passphrase)."""


@dataclass(frozen=True)
class RestoreReport:
    destination: Path
    files: int
    schema_revision: str
    created_at: str
    keyring_written: bool
    sessions_revoked: int = 0
    api_keys_revoked: int = 0


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


def _open_member(root_fd: int, rel: str, *, fixed: bool = False) -> tuple[int, int]:
    """The new file `rel` under the destination and the descriptor of its folder. `fixed`: a
    name of our own (the keyring file), not a member's."""
    if not fixed and not member_name_ok(rel):  # the archive's rule, on the way out as well
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
    except BaseException:
        os.close(folder)
        raise
    os.fchmod(out, 0o600)
    return out, folder


def _write_small(root_fd: int, rel: str, data: bytes) -> None:
    out, folder = _open_member(root_fd, rel, fixed=True)
    try:
        _write_all(out, data)
        os.fsync(out)
        os.fsync(folder)
    finally:
        os.close(out)
        os.close(folder)


class _Extractor:
    """Writes each member under the destination as the parser delivers it. The armored keyring
    member is not copied: the instance reads `keyring`, which is written from the keyring that
    the first pass opened, just before the database."""

    def __init__(self, root_fd: int, keyring_json: bytes | None) -> None:
        self._root = root_fd
        self._keyring_json = keyring_json
        self._out: int | None = None
        self._folder: int | None = None
        self._skip = False
        self.files = 0

    def begin(self, name: str, size: int) -> None:
        if name == KEYRING_MEMBER:
            self._skip = True
            return
        if name == DATABASE_MEMBER and self._keyring_json is not None:
            _write_small(self._root, KEYRING_FILE, self._keyring_json)
            self._keyring_json = None
            self.files += 1
        landing = DATABASE_TEMP if name == DATABASE_MEMBER else name
        self._out, self._folder = _open_member(self._root, landing, fixed=name == DATABASE_MEMBER)

    def data(self, chunk: bytes) -> None:
        if self._out is not None:
            _write_all(self._out, chunk)

    def end(self, name: str) -> None:
        if self._skip:
            self._skip = False
            return
        assert self._out is not None and self._folder is not None  # noqa: S101
        try:
            os.fsync(self._out)
            os.fsync(self._folder)
        finally:
            self.close()
        self.files += 1

    def close(self) -> None:
        for descriptor in (self._out, self._folder):
            if descriptor is not None:
                os.close(descriptor)
        self._out = self._folder = None


def _database_ok(path: Path) -> bool:
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return False
    try:
        return connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def restore_archive(
    archive: Path,
    destination: Path,
    passphrase_file: Path,
    *,
    live_data_dir: Path | None = None,
    keep_external_keyring: bool = False,
) -> RestoreReport:
    """Raises `PassphraseError`, `BackupArchiveError` (damaged, altered, wrong passphrase: one
    message) or `DestinationError`; in the first two cases nothing was written, except for a
    damage that only the second pass can see (the file changed between the passes, or the
    restored database fails its check), which leaves its folder as it is. `keep_external_keyring`:
    the instance reads its keyring from `SHELFHOLD_SECRET_KEY_FILE`; none is written into the
    destination."""
    try:
        passphrase = read_passphrase_file(passphrase_file)
    except ExportError as error:
        raise PassphraseError(str(error)) from None
    try:
        with open_archive_file(archive) as stream:
            # one descriptor for both passes: replacing the file by path between them changes
            # nothing for us, and a change in place is caught by the digest of the second pass
            checked = check_stream(stream, passphrase, live_data_dir, database=False)
            resolved = _check_destination(destination, live_data_dir)
            keyring_json = None if keep_external_keyring else checked.keyring.to_json()
            stream.seek(0)
            revoked = _write_all_members(
                stream,
                passphrase,
                resolved,
                keyring_json,
                checked.file_sha256,
                (checked.created_at, live_data_dir),
            )
    finally:
        del passphrase
    files = len(checked.members) - 1 + int(keyring_json is not None)  # `keyring.age` -> `keyring`
    return RestoreReport(
        resolved, files, checked.schema_revision, checked.created_at,
        keyring_written=keyring_json is not None,
        sessions_revoked=revoked[0], api_keys_revoked=revoked[1],
    )  # fmt: skip


@dataclass(frozen=True)
class _Live:
    """What the live database says about the identities, when it can be read."""

    passkeys: tuple[tuple[str, str, str, str | None], ...]  # id, name, created_at, revoked_at
    passwords: tuple[tuple[str, str], ...]  # user id, password hash
    counter: int


def _when(text: str | None) -> datetime | None:
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _read_live(live_data_dir: Path | None) -> _Live | None:
    """The live database, read only. None when it is not there or not readable (then nothing is
    carried over and the notice says so)."""
    if live_data_dir is None:
        return None
    path = live_data_dir / DATABASE_MEMBER
    try:
        connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        passkeys = tuple(
            (str(i), str(n), str(c), None if r is None else str(r))
            for i, n, c, r in connection.execute(
                "SELECT id, name, created_at, revoked_at FROM webauthn_credential"
            )
        )
        passwords = tuple(
            (str(i), str(h)) for i, h in connection.execute("SELECT id, password_hash FROM user")
        )
        row = connection.execute(
            "SELECT value FROM app_setting WHERE key = ?", (COUNTER_KEY,)
        ).fetchone()
    except sqlite3.Error:
        return None
    finally:
        connection.close()
    in_db = int(row[0]) if row and str(row[0]).isdigit() else 0
    return _Live(passkeys, passwords, max(in_db, IdentityCounter(live_data_dir).file_value() or 0))


def _passkey_changes(live: _Live, since: datetime | None, restored_ids: set[str]) -> list[str]:
    """The passkeys the live database created or revoked after the backup was made."""
    changes: list[str] = []
    for key_id, name, created, revoked in live.passkeys:
        made, closed = _when(created), _when(revoked)
        if since is not None and made is not None and made > since and key_id not in restored_ids:
            changes.append(f"created: {name}")
        if since is not None and closed is not None and closed > since:
            changes.append(f"revoked: {name}")
    return changes


def _revoke_credentials(
    database: Path, destination: Path, created_at: str, live_data_dir: Path | None
) -> tuple[int, int]:
    """On the restored database, before it can be started: no live session, no live API key, the
    notice written, the identity counter moved past the live one. When the live database can be
    read, its revocations of passkeys and its password hashes are carried over (a passkey revoked
    or a password reset since the backup stays so) and the passkeys it created or revoked since
    are listed in the notice."""
    now = datetime.now(UTC)
    live = _read_live(live_data_dir)
    counter = IdentityCounter(destination)
    engine = sa.create_engine(f"sqlite+pysqlite:///{database}", hide_parameters=True)
    sessions = keys = 0
    value: int | None = None
    try:
        with engine.begin() as connection:
            tables = set(sa.inspect(connection).get_table_names())
            if {"session", "api_key", "audit", "app_setting"} <= tables:
                changes: list[str] = []
                if live is not None:
                    restored = {
                        str(r[0])
                        for r in connection.exec_driver_sql("SELECT id FROM webauthn_credential")
                    }
                    for key_id, _name, _made, revoked in live.passkeys:
                        if revoked is not None:
                            connection.exec_driver_sql(
                                "UPDATE webauthn_credential SET revoked_at = ? "
                                "WHERE id = ? AND revoked_at IS NULL",
                                (revoked, key_id),
                            )
                    for user_id, password_hash in live.passwords:
                        connection.exec_driver_sql(
                            "UPDATE user SET password_hash = ? WHERE id = ?",
                            (password_hash, user_id),
                        )
                    changes = _passkey_changes(live, _when(created_at), restored)
                sessions, keys, value = revoke_all_credentials(
                    connection,
                    now=now,
                    notice=RestoreNoticeData(
                        now.isoformat(),
                        created_at,
                        0,
                        0,
                        passwords_carried=live is not None,
                        passkey_changes=tuple(changes),
                    ),
                    counter=counter,
                    counter_at_least=live.counter if live else 0,
                    keep_notice=False,  # this restore's notice, not one the backup carried
                )
    except sa.exc.SQLAlchemyError:
        raise RestoreIncompleteError from None  # no `shelfhold.db` yet: it cannot be started
    finally:
        engine.dispose()
    if value is not None:
        counter.publish(value)
    return sessions, keys


def _write_all_members(
    stream: BinaryIO,
    passphrase: str,
    resolved: Path,
    keyring_json: bytes | None,
    sha256: str,
    origin: tuple[str, Path | None],
) -> tuple[int, int]:
    root_fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    extractor = _Extractor(root_fd, keyring_json)
    reader = HashingReader(stream)
    try:
        try:
            read_archive(reader, passphrase, TarParser(extractor))
            reader.drain()
        except BackupArchiveError:
            raise RestoreIncompleteError from None
        extractor.close()
        if reader.digest.hexdigest() != sha256 or not _database_ok(resolved / DATABASE_TEMP):
            raise RestoreIncompleteError
        revoked = _revoke_credentials(resolved / DATABASE_TEMP, resolved, *origin)
        os.rename(DATABASE_TEMP, DATABASE_MEMBER, src_dir_fd=root_fd, dst_dir_fd=root_fd)
        os.fsync(root_fd)
        return revoked
    finally:
        extractor.close()
        os.close(root_fd)
