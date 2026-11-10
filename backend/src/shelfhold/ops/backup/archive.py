"""The backup archive: a tar of named members, framed by a manifest, encrypted as one age file.

The format (D3): one POSIX `ustar` tar (readable with `age -d | tar x` and nothing of ours),
written and read by the small functions below rather than by `tarfile`: the only entries are
regular files, so a link, a device, a folder or an extension header is not a thing this reader can
see, it is refused. The tar holds `BACKUP.json` (the manifest: format, instant, schema revision,
and the `sha256` and size of every other member) and the members themselves. The whole tar is
encrypted with age v1 and a scrypt recipient, the format of `secrets export`; a backup holds titles
and secrets, so nothing of it is readable without the passphrase. The master key travels once more
inside, as an armored age file of its own (`keyring.age`, `encrypt_export`), so the same bytes that
`secrets import` reads are the ones in the backup.

Nothing here touches a file system: this module turns bytes into members and back, and decides
which member names are acceptable. An archive is untrusted until it has been decrypted *and*
parsed *and* every member has matched its digest; any failure is one `BackupArchiveError` that
says nothing of the content (a generic message, as for a damaged `secrets export`).
"""

from __future__ import annotations

import hashlib
import json
import struct
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath
from typing import Final

from shelfhold.infrastructure.secrets.errors import ExportError
from shelfhold.infrastructure.secrets.export import decrypt_blob, encrypt_blob

__all__ = [
    "ARCHIVE_SUFFIX",
    "DATABASE_MEMBER",
    "FORMAT",
    "KEYRING_MEMBER",
    "MANIFEST_MEMBER",
    "MAX_ARCHIVE_BYTES",
    "BackupArchiveError",
    "Members",
    "Parsed",
    "decrypt_archive",
    "encrypt_archive",
    "pack",
    "unpack",
]

FORMAT: Final = "shelfhold-backup/1"
MANIFEST_MEMBER: Final = "BACKUP.json"
KEYRING_MEMBER: Final = "keyring.age"
DATABASE_MEMBER: Final = "shelfhold.db"
ARCHIVE_SUFFIX: Final = ".tar.age"
# The tar and its encryption are held in memory (pyrage encrypts bytes, not a stream), so the set
# is bounded: a database above this is refused with its own cause instead of exhausting the host.
MAX_ARCHIVE_BYTES: Final = 1024 * 1024 * 1024
MAX_MEMBERS: Final = 200_000
MAX_NAME_BYTES: Final = 240
MAX_MANIFEST_BYTES: Final = 64 * 1024 * 1024
# What a member may be called: nothing else is accepted, so an archive cannot place a file
# anywhere but in the places a restore knows.
FILES: Final = frozenset({DATABASE_MEMBER, "config.toml", KEYRING_MEMBER})
FOLDERS: Final = frozenset({"meta", "manifests", "docs"})
GENERIC = "the backup is damaged, or the passphrase is wrong"

Members = Mapping[str, bytes]


class BackupArchiveError(Exception):
    """An archive that cannot be trusted: one message, whatever is wrong with it."""

    def __init__(self) -> None:
        super().__init__(GENERIC)


@dataclass(frozen=True)
class Parsed:
    members: Members
    created_at: str
    schema_revision: str


def member_name_ok(name: str) -> bool:
    """A relative, plain, forward-slash path whose first component is a known one."""
    if not name or len(name.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES:
        return False
    if "\x00" in name or "\\" in name or name.startswith("/") or name.endswith("/"):
        return False
    path = PurePosixPath(name)
    parts = path.parts
    if path.is_absolute() or not parts or any(p in ("", ".", "..") for p in parts):
        return False
    if "/".join(parts) != name:  # `a//b`, `./a`
        return False
    if name == MANIFEST_MEMBER:
        return True
    if len(parts) == 1:
        return parts[0] in FILES
    return parts[0] in FOLDERS


BLOCK = 512
_ZERO = bytes(BLOCK)


def _octal(value: int, width: int) -> bytes:
    return f"{value:0{width - 1}o}".encode("ascii") + b"\x00"


def _header(name: str, size: int, stamp: int) -> bytes:
    raw = name.encode("utf-8")
    prefix = b""
    if len(raw) > 100:  # ustar: the last component(s) in `name`, the rest in `prefix`
        cut = raw.rfind(b"/", 0, 156)
        while cut > 0 and len(raw) - cut - 1 > 100:
            cut = raw.rfind(b"/", 0, cut)
        if cut <= 0 or len(raw) - cut - 1 > 100:
            raise ValueError("a member name too long for ustar")
        prefix, raw = raw[:cut], raw[cut + 1 :]
    head = bytearray(BLOCK)
    head[0 : len(raw)] = raw
    head[100:108] = _octal(0o600, 8)
    head[108:116] = _octal(0, 8)
    head[116:124] = _octal(0, 8)
    head[124:136] = _octal(size, 12)
    head[136:148] = _octal(stamp, 12)
    head[148:156] = b" " * 8
    head[156:157] = b"0"
    head[257:263] = b"ustar\x00"
    head[263:265] = b"00"
    head[345 : 345 + len(prefix)] = prefix
    head[148:156] = f"{sum(head):06o}".encode("ascii") + b"\x00 "
    return bytes(head)


def _field(block: bytes, start: int, end: int) -> bytes:
    return block[start:end].split(b"\x00", 1)[0]


def _number(block: bytes, start: int, end: int) -> int:
    text = block[start:end].rstrip(b"\x00 ")
    if not text or any(c not in b"01234567" for c in text):
        raise BackupArchiveError
    return int(text, 8)


def pack(members: Members, *, created_at: datetime, schema_revision: str) -> bytes:
    """The tar of the members and their manifest. Every name is checked on the way in, too."""
    listing: dict[str, dict[str, object]] = {}
    for name, data in members.items():
        if not member_name_ok(name) or name == MANIFEST_MEMBER:
            raise ValueError("a member name that no restore would accept")
        listing[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    manifest = json.dumps(
        {
            "format": FORMAT,
            "created_at": created_at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "schema_revision": schema_revision,
            "members": listing,
        },
        sort_keys=True,
    ).encode("utf-8")
    stamp = int(created_at.timestamp())
    out = bytearray()
    for name, data in [(MANIFEST_MEMBER, manifest), *sorted(members.items())]:
        out += _header(name, len(data), stamp)
        out += data
        out += bytes(-len(data) % BLOCK)
    out += _ZERO * 2
    return bytes(out)


def _read_entries(tar_bytes: bytes) -> dict[str, bytes]:
    """The regular files of a ustar tar, in order, nothing else: the first entry is the manifest,
    names are unique and acceptable, every checksum matches, the tar ends with zero blocks."""
    found: dict[str, bytes] = {}
    position, total = 0, len(tar_bytes)
    budget = MAX_ARCHIVE_BYTES
    while True:
        block = tar_bytes[position : position + BLOCK]
        if len(block) < BLOCK:
            raise BackupArchiveError  # truncated: no end marker
        if block == _ZERO:
            if tar_bytes[position + BLOCK : position + 2 * BLOCK] != _ZERO or any(
                tar_bytes[position + 2 * BLOCK :]
            ):
                raise BackupArchiveError
            return found
        if len(found) >= MAX_MEMBERS:
            raise BackupArchiveError
        checksum = _number(block, 148, 156)
        if checksum != sum(block[:148]) + 32 * 8 + sum(block[156:]):
            raise BackupArchiveError
        if block[156:157] != b"0" or block[257:263] != b"ustar\x00" or block[263:265] != b"00":
            raise BackupArchiveError  # a link, a folder, a device, an extension header: no
        size = _number(block, 124, 136)
        if size > budget or position + BLOCK + size > total:
            raise BackupArchiveError
        budget -= size
        try:
            name = _field(block, 0, 100).decode("utf-8")
            prefix = _field(block, 345, 500).decode("utf-8")
        except UnicodeDecodeError:
            raise BackupArchiveError from None
        name = f"{prefix}/{name}" if prefix else name
        if not member_name_ok(name) or name in found:
            raise BackupArchiveError
        if not found and name != MANIFEST_MEMBER:
            raise BackupArchiveError
        start = position + BLOCK
        found[name] = tar_bytes[start : start + size]
        position = start + size + (-size % BLOCK)


def unpack(tar_bytes: bytes) -> Parsed:
    """Parse and check an archive: regular files only, plain names, no duplicate, the manifest
    first, and exactly the members it lists with their digest and size. No path is joined here,
    no file is created: the caller receives bytes by name."""
    try:
        return _unpack(tar_bytes)
    except BackupArchiveError:
        raise
    except (OSError, ValueError, KeyError, TypeError, struct.error, MemoryError):
        raise BackupArchiveError from None


def _unpack(tar_bytes: bytes) -> Parsed:
    if len(tar_bytes) > MAX_ARCHIVE_BYTES + 3 * BLOCK:
        raise BackupArchiveError
    found = _read_entries(tar_bytes)
    manifest_raw = found.pop(MANIFEST_MEMBER, b"")
    if len(manifest_raw) > MAX_MANIFEST_BYTES:
        raise BackupArchiveError
    manifest = _manifest(manifest_raw)
    listing = manifest["members"]
    if not isinstance(listing, dict) or set(listing) != set(found):
        raise BackupArchiveError
    for name, data in found.items():
        entry = listing[name]
        if (
            not isinstance(entry, dict)
            or entry.get("size") != len(data)
            or entry.get("sha256") != hashlib.sha256(data).hexdigest()
        ):
            raise BackupArchiveError
    revision, created = manifest.get("schema_revision"), manifest.get("created_at")
    if not (isinstance(revision, str) and isinstance(created, str)) or DATABASE_MEMBER not in found:
        raise BackupArchiveError
    return Parsed(found, created, revision)


def _manifest(raw: bytes) -> dict[str, object]:
    document = json.loads(raw.decode("utf-8"))
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise BackupArchiveError
    return document


def encrypt_archive(tar_bytes: bytes, passphrase: str) -> bytes:
    """Binary age v1 with a scrypt recipient (no ASCII armor: the file is large)."""
    return encrypt_blob(tar_bytes, passphrase)


def decrypt_archive(blob: bytes, passphrase: str) -> bytes:
    """Every failure is the same generic error, the scrypt cost of the header bounded first."""
    if len(blob) > MAX_ARCHIVE_BYTES + 1024 * 1024:
        raise BackupArchiveError
    try:
        return decrypt_blob(blob, passphrase)
    except ExportError:
        raise BackupArchiveError from None
