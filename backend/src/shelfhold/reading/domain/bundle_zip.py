"""The layout of the block of a series: a stored ZIP, the same bytes every time (ADR-0053 §2, §3).

This module is pure. It knows the entries of the block (name, size, CRC-32) and writes what is
around the pages: the local headers, the central directory and the end record. The bytes of the
pages come from the caller, so the whole stream is a sequence of `bytes` (headers) and entry indexes
(a page follows), and its exact length is known before any page is read (`Content-Length`).

Everything that could vary is fixed: method 0 (stored), no data descriptor, no flag, DOS date and
time at 1980-01-01 00:00:00, no attribute, no comment, no extra field except the one of entry 1.
Zip64 is used if and only if the block has more than 65 535 entries; a block is strictly smaller
than 2³¹ bytes (32-bit readers), so no offset or size ever needs it.

The first entry carries, in the extra field of its local header, the full fingerprint of the
version (a private header id, which MuPDF skips without reading). KOReader hashes twelve samples of
1 KiB and stops at the first empty one: two versions that differ by a chapter added at the end share
a hash unless the first KiB tells them apart. `LayoutStream` is a seekable view of the block that
reads only what is asked, so the KOReader digest is computed without materialising the file.
"""

import io
import re
import struct
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "IDENTITY_BYTES",
    "MAX_PART_BYTES",
    "ZIP64_ENTRIES",
    "BundleZip",
    "LayoutStream",
    "PartTooLargeError",
    "UnsafeMemberNameError",
    "ZipEntry",
    "chapter_folder",
    "check_member_name",
    "page_name",
]

MAX_PART_BYTES: Final = 2**31 - 1  # strictly under 2 GiB: e-readers are 32-bit
ZIP64_ENTRIES: Final = 65_535  # more entries than this and the end record says Zip64
IDENTITY_BYTES: Final = 32
_IDENTITY_ID: Final = 0x4853  # a private header id ("SH"): readers skip what they do not know
_DOS_TIME: Final = 0
_DOS_DATE: Final = (1 << 5) | 1  # 1980-01-01
_MAX_NAME: Final = 255
_MAX_NUMBER: Final = 24
_NAME = re.compile(r"[0-9]{5} ch[A-Za-z0-9._-]{0,24}/[0-9]{5}\.[a-z0-9]{1,5}")
_NUMBER_KEPT = re.compile(r"[^A-Za-z0-9._-]+")
_EXTENSION = re.compile(r"[a-z0-9]{1,5}")
_U32 = 2**32


class UnsafeMemberNameError(ValueError):
    """A member name that could reach outside the block, or is not one this block writes."""


class PartTooLargeError(ValueError):
    """The block would be 2³¹ bytes or more."""


def check_member_name(name: str) -> str:
    """The name if it is `{rank:05d} ch{number}/{page:05d}.{ext}` in ASCII, else an error: no
    `..`, no absolute path, no backslash, no empty segment, no control character, no non-ASCII."""
    if not name or len(name) > _MAX_NAME or not name.isascii() or "\\" in name:
        raise UnsafeMemberNameError(f"member name refused: {name[:40]!r}")
    if name.startswith("/") or ".." in name.split("/") or "//" in name:
        raise UnsafeMemberNameError(f"member name refused: {name[:40]!r}")
    if _NAME.fullmatch(name) is None:
        raise UnsafeMemberNameError(f"member name refused: {name[:40]!r}")
    return name


def chapter_folder(rank: int, number: str) -> str:
    """`{rank:05d} ch{number}`: the rank first (it fixes the natural order), the number of the
    chapter cleaned to ASCII and cut to 24 characters, never a title (the table of contents that
    KOReader builds from the folders shows it)."""
    kept = _NUMBER_KEPT.sub("", number.encode("ascii", "ignore").decode())
    while ".." in kept:
        kept = kept.replace("..", ".")
    kept = kept.strip(".")[:_MAX_NUMBER]
    return f"{rank:05d} ch{kept}"


def page_name(page: int, extension: str) -> str:
    ext = extension.lower().lstrip(".")
    if _EXTENSION.fullmatch(ext) is None:
        raise UnsafeMemberNameError(f"extension refused: {extension[:10]!r}")
    return f"{page:05d}.{ext}"


@dataclass(frozen=True, slots=True)
class ZipEntry:
    """One page of the block: its name, the size and the CRC-32 of its bytes."""

    name: str
    size: int
    crc32: int

    def __post_init__(self) -> None:
        if not 0 <= self.size < _U32 or not 0 <= self.crc32 < _U32:
            raise ValueError("size and CRC-32 are 32-bit values")


Piece = bytes | int  # bytes of a header, or the index of the entry whose data follows
ReadEntry = Callable[[int, int, int], bytes]  # (entry index, start, length) -> bytes


class BundleZip:
    """The headers and offsets of a block. `pieces()` is the stream; `size` is its exact length."""

    def __init__(self, entries: Sequence[ZipEntry], identity: bytes) -> None:
        if len(identity) != IDENTITY_BYTES:
            raise ValueError(f"the identity is {IDENTITY_BYTES} bytes")
        self.entries = tuple(entries)
        self.identity = identity
        self.names = [check_member_name(entry.name) for entry in self.entries]
        self.zip64 = len(self.entries) > ZIP64_ENTRIES
        self._offsets: list[int] = []
        position = 0
        for index, entry in enumerate(self.entries):
            self._offsets.append(position)
            position += len(self._local(index)) + entry.size
        self._central = self._central_directory(position)
        self._central_offset = position
        self.size = position + len(self._central)
        if self.size > MAX_PART_BYTES:
            raise PartTooLargeError(f"a block of {self.size} bytes is not under 2^31")

    # -- headers --------------------------------------------------------------------------------
    def _local(self, index: int) -> bytes:
        entry = self.entries[index]
        name = self.names[index].encode("ascii")
        extra = (
            struct.pack("<HH", _IDENTITY_ID, IDENTITY_BYTES) + self.identity if index == 0 else b""
        )
        return (
            struct.pack(
                "<4sHHHHHIIIHH",
                b"PK\x03\x04",
                20,
                0,
                0,
                _DOS_TIME,
                _DOS_DATE,
                entry.crc32,
                entry.size,
                entry.size,
                len(name),
                len(extra),
            )
            + name
            + extra
        )

    def _central_directory(self, offset: int) -> bytes:
        parts = []
        for index, entry in enumerate(self.entries):
            name = self.names[index].encode("ascii")
            parts.append(
                struct.pack(
                    "<4sHHHHHHIIIHHHHHII",
                    b"PK\x01\x02",
                    20,
                    20,
                    0,
                    0,
                    _DOS_TIME,
                    _DOS_DATE,
                    entry.crc32,
                    entry.size,
                    entry.size,
                    len(name),
                    0,
                    0,
                    0,
                    0,
                    0,
                    self._offsets[index],
                )
                + name
            )
        directory = b"".join(parts)
        count = len(self.entries)
        if not self.zip64:
            return directory + struct.pack(
                "<4sHHHHIIH", b"PK\x05\x06", 0, 0, count, count, len(directory), offset, 0
            )
        end64 = offset + len(directory)
        record = struct.pack(
            "<4sQHHIIQQQQ", b"PK\x06\x06", 44, 45, 45, 0, 0, count, count, len(directory), offset
        )
        locator = struct.pack("<4sIQI", b"PK\x06\x07", 0, end64, 1)
        end = struct.pack(
            "<4sHHHHIIH", b"PK\x05\x06", 0, 0, 0xFFFF, 0xFFFF, len(directory), offset, 0
        )
        return directory + record + locator + end

    # -- the stream -----------------------------------------------------------------------------
    def pieces(self) -> Iterator[Piece]:
        for index in range(len(self.entries)):
            yield self._local(index)
            if self.entries[index].size:
                yield index
        yield self._central

    def read_range(self, offset: int, length: int, read_entry: ReadEntry) -> bytes:
        """Bytes `[offset, offset + length)`; page data is read only where it falls."""
        end = min(self.size, offset + max(0, length))
        out = bytearray()
        position = 0
        for index, entry in enumerate(self.entries):
            if position >= end:
                break
            header = self._local(index)
            self._take(out, header, position, offset, end)
            position += len(header)
            span = (max(position, offset), min(position + entry.size, end))
            if span[0] < span[1]:
                out += read_entry(index, span[0] - position, span[1] - span[0])
            position += entry.size
        if position < end:
            self._take(out, self._central, position, offset, end)
        return bytes(out)

    @staticmethod
    def _take(out: bytearray, chunk: bytes, position: int, start: int, end: int) -> None:
        lo, hi = max(position, start), min(position + len(chunk), end)
        if lo < hi:
            out += chunk[lo - position : hi - position]


class LayoutStream(io.RawIOBase):
    """A read-only seekable view of the block. KOReader's digest reads twelve samples through it."""

    def __init__(self, layout: BundleZip, read_entry: ReadEntry) -> None:
        super().__init__()
        self._layout, self._read_entry, self._position = layout, read_entry, 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        base = {io.SEEK_SET: 0, io.SEEK_CUR: self._position, io.SEEK_END: self._layout.size}[whence]
        self._position = max(0, base + offset)
        return self._position

    def read(self, size: int = -1) -> bytes:
        want = self._layout.size - self._position if size < 0 else size
        data = self._layout.read_range(self._position, want, self._read_entry)
        self._position += len(data)
        return data

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)
