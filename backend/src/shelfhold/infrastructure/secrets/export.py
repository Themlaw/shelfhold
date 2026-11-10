"""Keyring export: an age v1 file with a scrypt recipient, ASCII-armored (ENF-SEC-005).

The scrypt layer is `age_scrypt` (age v1 written and read here, with a fixed work factor, so that
no timing of the host decides whether a file can be opened); the x25519 stream of the backup body
is `pyrage`. This module reads the passphrase and frames the result. The same functions are what
the integrated backup calls, so the export and the key in the backup are one format. The
passphrase comes from a file, never from the command line.
"""

import base64
import binascii
import os
import stat
import textwrap
from pathlib import Path
from typing import Protocol

import pyrage

from shelfhold.infrastructure.secrets import age_scrypt
from shelfhold.infrastructure.secrets.errors import ExportError, KeyringCorruptError
from shelfhold.infrastructure.secrets.keyring import Keyring

ARMOR_BEGIN = "-----BEGIN AGE ENCRYPTED FILE-----"
ARMOR_END = "-----END AGE ENCRYPTED FILE-----"
ARMOR_COLUMNS = 64
MIN_PASSPHRASE_CHARS = 12
MAX_PASSPHRASE_FILE_BYTES = 4096
MAX_EXPORT_BYTES = 1024 * 1024  # a keyring of 1000 keys armored is ~170 KiB


SECRETS_MOUNT = "/run/secrets"  # where Docker puts a secret (owned by root, mode 0444)
VIRTUAL_TREES = ("/proc/", "/sys/", "/dev/")  # files that are not files (`/proc/self/environ`)


def _open_regular(path: Path, what: str, *, nofollow: bool = False) -> tuple[int, os.stat_result]:
    """A descriptor on a regular file; a pipe or a device is refused without being read, so a
    FIFO cannot make the command wait. `nofollow`: the last component is not a link. The
    permissions are the caller's to judge."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (os.O_NOFOLLOW if nofollow else 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ExportError(f"cannot read the {what}: {exc.strerror}") from None
    try:
        status = os.fstat(descriptor)
        if not stat.S_ISREG(status.st_mode):
            raise ExportError(f"the {what} is not a regular file")
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor, status


def _read_bounded(descriptor: int, limit: int, what: str) -> bytes:
    try:
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read(limit + 1)
    except OSError as exc:
        raise ExportError(f"cannot read the {what}: {exc.strerror}") from None


def _check_passphrase_permissions(path: Path, status: os.stat_result) -> None:
    """Owner-only, as the keyring is. The one exception is a Docker secret: a file root owns
    under `/run/secrets` (0444 by design) only has to be out of reach of writers."""
    if status.st_uid not in (0, os.geteuid()):
        raise ExportError("the passphrase file belongs to another user")
    others = 0o077
    if status.st_uid == 0 and os.geteuid() != 0:
        if not os.path.realpath(path).startswith(SECRETS_MOUNT + "/"):
            raise ExportError(
                f"the passphrase file belongs to root outside {SECRETS_MOUNT}; "
                f"copy it and fix it with: chmod 600 {path}"
            )
        others = 0o022
    if status.st_mode & others:
        raise ExportError(
            f"the passphrase file is accessible by other users; fix it with: chmod 600 {path}"
        )


def read_passphrase_file(path: Path) -> str:
    """The passphrase is the file's content without its final line break."""
    if os.path.realpath(path).startswith(VIRTUAL_TREES):
        raise ExportError("the passphrase file is not a regular file")
    descriptor, status = _open_regular(path, "passphrase file", nofollow=True)
    try:
        _check_passphrase_permissions(path, status)
    except BaseException:
        os.close(descriptor)
        raise
    raw = _read_bounded(descriptor, MAX_PASSPHRASE_FILE_BYTES, "passphrase file")
    if len(raw) > MAX_PASSPHRASE_FILE_BYTES:
        raise ExportError("the passphrase file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ExportError("the passphrase file is not UTF-8 text") from None
    text = text.removesuffix("\n").removesuffix("\r")
    if len(text) < MIN_PASSPHRASE_CHARS:
        raise ExportError(f"the passphrase must have at least {MIN_PASSPHRASE_CHARS} characters")
    return text


def read_export_file(path: Path, *, nofollow: bool = False) -> str:
    """An export file as text, read within a bound: it is encrypted and signed by nobody, so
    nothing about it is trusted before it has been decrypted and parsed."""
    descriptor, _status = _open_regular(path, "export file", nofollow=nofollow)
    raw = _read_bounded(descriptor, MAX_EXPORT_BYTES, "export file")
    if len(raw) > MAX_EXPORT_BYTES:
        raise ExportError("the export file is too large")
    try:
        return raw.decode("ascii")
    except UnicodeDecodeError:
        raise ExportError("not an armored age file") from None


def armor(binary: bytes) -> str:
    body = textwrap.wrap(base64.b64encode(binary).decode("ascii"), ARMOR_COLUMNS)
    return "\n".join([ARMOR_BEGIN, *body, ARMOR_END]) + "\n"


def dearmor(text: str) -> bytes:
    lines = text.strip().splitlines()
    if len(lines) < 3 or lines[0] != ARMOR_BEGIN or lines[-1] != ARMOR_END:
        raise ExportError("not an armored age file")
    try:
        return base64.b64decode("".join(lines[1:-1]), validate=True)
    except binascii.Error:
        raise ExportError("the armor is not valid base64") from None


def encrypt_export(keyring: Keyring, passphrase: str) -> str:
    """The whole keyring (retired keys included) as an armored age file."""
    return armor(age_scrypt.seal(keyring.to_json(), passphrase))


def encrypt_blob(clear: bytes, passphrase: str) -> bytes:
    """Binary age v1 with a scrypt recipient, for the integrated backup (EF-INT-015): the format
    of `encrypt_export` without the ASCII armor, which a large archive does not want."""
    return age_scrypt.seal(clear, passphrase)


def decrypt_blob(binary: bytes, passphrase: str) -> bytes:
    """The clear of a binary age file; whatever is wrong is the same `ExportError` as for an
    export, and a cost above the bound is the `WorkFactorError` that says so."""
    return age_scrypt.unseal(binary, passphrase)


class ByteSource(Protocol):
    def read(self, size: int = -1, /) -> bytes: ...


class ByteSink(Protocol):
    def write(self, data: bytes, /) -> object: ...


def new_stream_key() -> tuple[str, str]:
    """A throw-away age key for one backup: `(secret, recipient)`, both text. The body of a backup
    is encrypted to the recipient as a stream (bounded memory); the secret travels in the header,
    sealed by the passphrase, and is never stored anywhere else."""
    identity = pyrage.x25519.Identity.generate()
    return str(identity), str(identity.to_public())


def encrypt_stream(source: ByteSource, sink: ByteSink, recipient: str) -> None:
    """Binary age v1 to an x25519 recipient, read and written in chunks."""
    try:
        pyrage.encrypt_io(source, sink, [pyrage.x25519.Recipient.from_str(recipient)])
    except (pyrage.EncryptError, pyrage.RecipientError, ValueError):
        raise ExportError("the stream could not be encrypted") from None


def decrypt_stream(source: ByteSource, sink: ByteSink, secret: str) -> None:
    """The clear of a stream made by `encrypt_stream`, handed to `sink` in chunks. Any failure
    (truncated, altered, wrong key) is the same `ExportError`; `sink` may already hold a prefix of
    the clear, which only its owner may trust once this has returned."""
    caught: list[BaseException] = []

    class Guard:
        def write(self, data: bytes, /) -> int:
            try:
                sink.write(data)
            except BaseException as error:  # the sink's own failure is not a damaged stream
                caught.append(error)
                raise
            return len(data)

    try:
        pyrage.decrypt_io(source, Guard(), [pyrage.x25519.Identity.from_str(secret)])
    except (pyrage.DecryptError, pyrage.IdentityError, ValueError, OSError):
        if caught:
            raise caught[0] from None
        raise ExportError("wrong key or damaged stream") from None


def decrypt_export(text: str, passphrase: str) -> Keyring:
    """The keyring of an export. Whatever is wrong (not age, wrong passphrase, damaged, not a
    keyring) is one `ExportError` that says nothing of the content: the file is untrusted."""
    if len(text) > MAX_EXPORT_BYTES:
        raise ExportError("the export file is too large")
    clear = age_scrypt.unseal(dearmor(text), passphrase)
    try:
        return Keyring.from_json(clear, source="export")
    except KeyringCorruptError:
        raise ExportError("the file does not hold a valid keyring") from None
