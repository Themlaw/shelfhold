"""Keyring export: an age v1 file with a scrypt recipient, ASCII-armored (ENF-SEC-005).

The cryptography is `pyrage` (age) and nothing else: this module only reads the passphrase and
frames the result. The same function is what the integrated backup of M1b will call, so the
export and the key in the backup are one format. The passphrase comes from a file, never from
the command line.
"""

import base64
import binascii
import textwrap
from pathlib import Path

import pyrage

from shelfhold.infrastructure.secrets.errors import ExportError
from shelfhold.infrastructure.secrets.keyring import Keyring

ARMOR_BEGIN = "-----BEGIN AGE ENCRYPTED FILE-----"
ARMOR_END = "-----END AGE ENCRYPTED FILE-----"
ARMOR_COLUMNS = 64
MIN_PASSPHRASE_CHARS = 12
MAX_PASSPHRASE_FILE_BYTES = 4096


def read_passphrase_file(path: Path) -> str:
    """The passphrase is the file's content without its final line break."""
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_PASSPHRASE_FILE_BYTES + 1)
    except OSError as exc:
        raise ExportError(f"cannot read the passphrase file: {exc.strerror}") from None
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
    return armor(pyrage.passphrase.encrypt(keyring.to_json(), passphrase))


def decrypt_export(text: str, passphrase: str) -> Keyring:
    binary = dearmor(text)
    try:
        clear = pyrage.passphrase.decrypt(binary, passphrase)
    except (pyrage.DecryptError, ValueError):
        raise ExportError("wrong passphrase or damaged file") from None
    return Keyring.from_json(clear, source="export")
