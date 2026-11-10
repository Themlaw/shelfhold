"""The backup archive: a tar of named members, framed by a manifest, encrypted as a stream.

The format (D3, `shelfhold-backup/2`): one file `shelfhold-backup-<UTC>.tar.age` that holds

1. a first line `shelfhold-backup/2 <N>` (text, then a newline);
2. `N` bytes: a binary age file, scrypt-sealed by the passphrase (the format of
   `secrets export`), whose clear is a throw-away age secret key made for this backup alone;
3. the rest: the tar, as one binary age file encrypted to the public half of that key.

Why two layers: pyrage streams an x25519 body in constant memory but not a scrypt one with
all of it at once; the passphrase protects the small key, the key protects the body. The standard
`age` command opens it in two steps (see `docs/admin/sauvegarde.md`), with nothing of ours.

The tar is a POSIX `ustar`, written and read by the small pieces below rather than by `tarfile`:
the only entries are regular files, so a link, a device, a folder or an extension header is not a
thing this reader can see, it is refused. It holds `BACKUP.json` (the manifest: format, instant,
schema revision, and the `sha256` and size of every other member) and then the members, sorted.
The master key travels once more inside, as an armored age file of its own (`keyring.age`,
`encrypt_export`), so the same bytes that `secrets import` reads are in the backup.

Both directions are streams: `iter_tar` yields the tar in chunks (a big member is a path, read
twice, once to list it and once to write it), `TarParser` is fed the clear in chunks and hands
each member's bytes to a handler as they come, checking the manifest's digest on the way. The
memory of a run, a verify or a restore does not depend on the size of the database.

Nothing here touches a file system but the sources it is given to read: this module turns bytes
into members and back, and decides which member names are acceptable. An archive is untrusted
until it has been decrypted *and* parsed *and* every member has matched its digest; any failure is
one `BackupArchiveError` that says nothing of the content.
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Final, Protocol

from shelfhold.infrastructure.logging import register_secret_value
from shelfhold.infrastructure.secrets.errors import ExportError
from shelfhold.infrastructure.secrets.export import (
    ByteSink,
    decrypt_blob,
    decrypt_stream,
    encrypt_blob,
    encrypt_stream,
    new_stream_key,
)

__all__ = [
    "ARCHIVE_MAGIC",
    "ARCHIVE_SUFFIX",
    "DATABASE_MEMBER",
    "FORMAT",
    "KEYRING_MEMBER",
    "MANIFEST_MEMBER",
    "MAX_TAR_BYTES",
    "ArchiveSource",
    "BackupArchiveError",
    "MemberHandler",
    "Parsed",
    "Source",
    "TarParser",
    "iter_tar",
    "member_name_ok",
    "read_archive",
    "write_archive",
]

FORMAT: Final = "shelfhold-backup/2"
ARCHIVE_MAGIC: Final = b"shelfhold-backup/2 "
MANIFEST_MEMBER: Final = "BACKUP.json"
KEYRING_MEMBER: Final = "keyring.age"
DATABASE_MEMBER: Final = "shelfhold.db"
ARCHIVE_SUFFIX: Final = ".tar.age"
# The one upper bound left: the clear tar of one backup, so that a stream with no end (a forged
# or looping file) is cut off. 32 GiB is far above any database of the supported scale.
MAX_TAR_BYTES: Final = 32 * 1024**3
MAX_KEY_BLOB: Final = 4096
MAX_MEMBERS: Final = 200_000
MAX_NAME_BYTES: Final = 240
MAX_MANIFEST_BYTES: Final = 64 * 1024 * 1024
CHUNK: Final = 1024 * 1024
# What a member may be called: nothing else is accepted, so an archive cannot place a file
# anywhere but in the places a restore knows.
FILES: Final = frozenset({DATABASE_MEMBER, "config.toml", KEYRING_MEMBER})
FOLDERS: Final = frozenset({"meta", "manifests", "docs"})
GENERIC = "the backup is damaged, or the passphrase is wrong"

Source = bytes | Path


class BackupArchiveError(Exception):
    """An archive that cannot be trusted: one message, whatever is wrong with it."""

    def __init__(self) -> None:
        super().__init__(GENERIC)


@dataclass(frozen=True)
class Parsed:
    members: Mapping[str, int]  # name -> size
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


def _open_source(path: Path) -> BinaryIO:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    return os.fdopen(descriptor, "rb")


def _chunks(source: Source) -> Iterator[bytes]:
    if isinstance(source, bytes):
        yield source
        return
    with _open_source(source) as stream:
        while block := stream.read(CHUNK):
            yield block


def _measure(source: Source) -> tuple[str, int]:
    digest, size = hashlib.sha256(), 0
    for block in _chunks(source):
        digest.update(block)
        size += len(block)
    return digest.hexdigest(), size


def iter_tar(
    sources: Mapping[str, Source], *, created_at: datetime, schema_revision: str
) -> Iterator[bytes]:
    """The tar of the sources and their manifest, in chunks. Every name is checked on the way in;
    a path source is measured first and must give the same bytes when it is written (a source
    that changed under us is a `ValueError`, not a silently wrong backup)."""
    listing: dict[str, dict[str, object]] = {}
    for name, source in sources.items():
        if not member_name_ok(name) or name == MANIFEST_MEMBER:
            raise ValueError("a member name that no restore would accept")
        sha, size = _measure(source)
        listing[name] = {"sha256": sha, "size": size}
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
    for name, source in [(MANIFEST_MEMBER, manifest), *sorted(sources.items())]:
        size = int(listing[name]["size"]) if name in listing else len(manifest)  # type: ignore[call-overload]
        yield _header(name, size, stamp)
        digest, seen = hashlib.sha256(), 0
        for block in _chunks(source):
            digest.update(block)
            seen += len(block)
            yield block
        if name in listing and (seen != size or digest.hexdigest() != listing[name]["sha256"]):
            raise ValueError("a source changed while it was written")
        yield bytes(-size % BLOCK)
    yield _ZERO * 2


class MemberHandler(Protocol):
    """What a reader does with the members as they arrive (the manifest is not one of them)."""

    def begin(self, name: str, size: int) -> None: ...
    def data(self, chunk: bytes) -> None: ...
    def end(self, name: str) -> None: ...


class TarParser:
    """A push parser for the tar: feed it the clear (`write`), then `finish()`. Regular files
    only, plain names, no duplicate, the manifest first, and exactly the members it lists with
    their size and digest. The handler receives each member's bytes as they come, *before* its
    digest is known to match: it may write them somewhere private, and may only trust them once
    `end(name)` has been called (the digest matched) and `finish()` has returned."""

    def __init__(self, handler: MemberHandler) -> None:
        self._handler = handler
        self._buffer = bytearray()
        self._state = "header"  # header | body | pad | tail
        self._name = ""
        self._left = 0
        self._pad = 0
        self._digest = hashlib.sha256()
        self._manifest = bytearray()
        self._listing: dict[str, dict[str, object]] | None = None
        self._seen: dict[str, int] = {}
        self._entries = 0
        self._total = 0
        self._zeros = 0
        self._created = ""
        self._revision = ""

    def write(self, data: bytes) -> int:
        try:
            self._feed(memoryview(data))
        except BackupArchiveError:
            raise
        except (ValueError, KeyError, TypeError, struct.error):
            raise BackupArchiveError from None
        return len(data)

    def _feed(self, view: memoryview) -> None:
        position = 0
        while position < len(view):
            if self._state == "header":
                take = min(BLOCK - len(self._buffer), len(view) - position)
                self._buffer += view[position : position + take]
                position += take
                if len(self._buffer) == BLOCK:
                    block = bytes(self._buffer)
                    self._buffer.clear()
                    self._on_header(block)
            elif self._state == "body":
                take = min(self._left, len(view) - position)
                self._on_body(bytes(view[position : position + take]))
                position += take
                self._left -= take
                if self._left == 0:
                    self._finish_member()
            elif self._state == "pad":
                take = min(self._pad, len(view) - position)
                if any(view[position : position + take]):
                    raise BackupArchiveError
                position += take
                self._pad -= take
                if self._pad == 0:
                    self._state = "header"
            else:  # tail: nothing but zeros may follow the end marker
                if any(view[position:]):
                    raise BackupArchiveError
                self._zeros += len(view) - position
                position = len(view)

    def _on_header(self, block: bytes) -> None:
        if block == _ZERO:
            if self._entries == 0:
                raise BackupArchiveError
            self._state, self._zeros = "tail", BLOCK
            return
        if self._entries >= MAX_MEMBERS:
            raise BackupArchiveError
        if _number(block, 148, 156) != sum(block[:148]) + 32 * 8 + sum(block[156:]):
            raise BackupArchiveError
        if block[156:157] != b"0" or block[257:263] != b"ustar\x00" or block[263:265] != b"00":
            raise BackupArchiveError  # a link, a folder, a device, an extension header: no
        size = _number(block, 124, 136)
        self._total += BLOCK + size + (-size % BLOCK)
        if self._total > MAX_TAR_BYTES:
            raise BackupArchiveError
        try:
            name = _field(block, 0, 100).decode("utf-8")
            prefix = _field(block, 345, 500).decode("utf-8")
        except UnicodeDecodeError:
            raise BackupArchiveError from None
        name = f"{prefix}/{name}" if prefix else name
        if (
            not member_name_ok(name)
            or name in self._seen
            or (self._entries == 0) != (name == MANIFEST_MEMBER)
        ):
            raise BackupArchiveError
        self._entries += 1
        self._name, self._left, self._pad = name, size, -size % BLOCK
        self._digest = hashlib.sha256()
        if name == MANIFEST_MEMBER:
            if size > MAX_MANIFEST_BYTES:
                raise BackupArchiveError
        else:
            assert self._listing is not None  # noqa: S101 (the manifest came first)
            entry = self._listing.get(name)
            if entry is None or entry.get("size") != size:
                raise BackupArchiveError
            self._handler.begin(name, size)
        self._seen[name] = size
        self._state = "body"
        if size == 0:
            self._finish_member()

    def _on_body(self, chunk: bytes) -> None:
        if self._name == MANIFEST_MEMBER:
            self._manifest += chunk
            return
        self._digest.update(chunk)
        self._handler.data(chunk)

    def _finish_member(self) -> None:
        if self._name == MANIFEST_MEMBER:
            self._read_manifest(bytes(self._manifest))
            self._manifest = bytearray()
        else:
            assert self._listing is not None  # noqa: S101
            if self._listing[self._name].get("sha256") != self._digest.hexdigest():
                raise BackupArchiveError
            self._handler.end(self._name)
        self._state = "pad" if self._pad else "header"

    def _read_manifest(self, raw: bytes) -> None:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BackupArchiveError from None
        if not isinstance(document, dict) or document.get("format") != FORMAT:
            raise BackupArchiveError
        listing = document.get("members")
        revision, created = document.get("schema_revision"), document.get("created_at")
        if not (
            isinstance(listing, dict)
            and isinstance(revision, str)
            and isinstance(created, str)
            and DATABASE_MEMBER in listing
            and all(member_name_ok(n) and isinstance(e, dict) for n, e in listing.items())
        ):
            raise BackupArchiveError
        self._listing, self._created, self._revision = listing, created, revision

    def finish(self) -> Parsed:
        """The stream ended: it must have ended where the tar does."""
        if self._state != "tail" or self._zeros < 2 * BLOCK or self._listing is None:
            raise BackupArchiveError
        seen = {n: s for n, s in self._seen.items() if n != MANIFEST_MEMBER}
        if set(seen) != set(self._listing):
            raise BackupArchiveError
        return Parsed(seen, self._created, self._revision)


class _ChunkReader:
    """A file-like `read` over an iterator of chunks (what `encrypt_stream` pulls from)."""

    def __init__(self, chunks: Iterator[bytes]) -> None:
        self._chunks = chunks
        self._rest = b""

    def read(self, size: int = -1, /) -> bytes:
        if size < 0:
            size = 1 << 62
        while len(self._rest) < size:
            block = next(self._chunks, None)
            if block is None:
                break
            self._rest += block
        out, self._rest = self._rest[:size], self._rest[size:]
        return out


def write_archive(sink: ByteSink, chunks: Iterator[bytes], passphrase: str) -> None:
    """The header, the sealed key and the encrypted tar, written to `sink` as they are made. The
    seal is refused (`WorkFactorError`) when age sized its scrypt above what `read_archive`
    accepts: an archive that could not be opened is no backup."""
    secret, recipient = new_stream_key()
    register_secret_value(secret)  # never in a log line, whatever happens next
    try:
        sealed = encrypt_blob(secret.encode("ascii"), passphrase)
        sink.write(ARCHIVE_MAGIC + str(len(sealed)).encode("ascii") + b"\n")
        sink.write(sealed)
        encrypt_stream(_ChunkReader(chunks), sink, recipient)
    finally:
        del secret


def has_archive_magic(head: bytes) -> bool:
    return head.startswith(ARCHIVE_MAGIC)


class ArchiveSource(Protocol):
    def readline(self, size: int = -1, /) -> bytes: ...
    def read(self, size: int = -1, /) -> bytes: ...


def read_archive(source: ArchiveSource, passphrase: str, parser: TarParser) -> Parsed:
    """Open the key with the passphrase, then decrypt the body into `parser`. Every failure of
    the archive itself is the one `BackupArchiveError`; a failure of the handler is its own."""
    try:
        line = source.readline(len(ARCHIVE_MAGIC) + 12)
        if not line.startswith(ARCHIVE_MAGIC) or not line.endswith(b"\n"):
            raise BackupArchiveError
        digits = line[len(ARCHIVE_MAGIC) : -1]
        # ASCII digits and nothing else: no sign, no space, no underscore, no leading zero
        if not (digits.isdigit() and digits.isascii() and digits[:1] != b"0" and len(digits) <= 4):
            raise BackupArchiveError
        size = int(digits)
        if size > MAX_KEY_BLOB:
            raise BackupArchiveError
        sealed = source.read(size)
        if len(sealed) != size:
            raise BackupArchiveError
        secret = decrypt_blob(sealed, passphrase).decode("ascii")
        register_secret_value(secret)
        try:
            decrypt_stream(source, parser, secret)
        finally:
            del secret
    except (ExportError, ValueError, UnicodeDecodeError):
        raise BackupArchiveError from None
    return parser.finish()
