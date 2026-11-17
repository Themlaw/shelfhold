"""The gzip and protobuf reader of a Mihon backup, written by hand (EF-INT-008, D14).

A backup is one gzip member holding one protobuf message. The file comes from outside, so every
number that could grow is bounded, and a file over a bound is refused **whole**, with a closed word,
never cut and never half read:

* the compressed size and the decompressed size (a gzip bomb stops at the bound: the decompressor
  is asked for at most that many bytes, so the memory is bounded too);
* the depth of nested groups (the only recursion protobuf allows in what is skipped), and the number
  of fields read over the whole file, those skipped included;
* the length of a string in a field that is read.

Only the fields Shelfhold uses are decoded: the series (source id, address, title, favorite), its
chapters (address, read flag, last page, number) and its tracker entries (tracker, remote id,
last chapter). Everything else, a field of a later version included, is skipped by its length
without being read; the names of the sources and the preferences (which hold tracker tokens) are
skipped and never kept. A top-level field this build does not know is a later generation of the
format and is refused by name (`unknown_schema`), because it may change what the known ones mean.

Nothing here opens a path, asks the network or imports anything outside the standard library: the
module is run, in a subprocess with no network and bounded memory and time, by `sandbox`.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import asdict, dataclass, field
from typing import Any, Final

INVALID: Final = "import.tachibk_invalid"
SCHEMA: Final = "import.tachibk_schema"
UNAVAILABLE: Final = "import.tachibk_unavailable"

# Top-level fields of the backup message, by generation of the format. The format carries no
# version number, so a generation is recognised by the fields it uses: 1 manga, 2 categories,
# 100 sources, 101 broken sources, 104 preferences, 105 source preferences, 106 extension repos.
SCHEMA_VERSIONS: Final[dict[str, frozenset[int]]] = {
    "mihon-1": frozenset({1, 2, 100, 101}),
    "mihon-2": frozenset({1, 2, 100, 101, 104, 105}),
    "mihon-3": frozenset({1, 2, 100, 101, 104, 105, 106}),
}

_MIB: Final = 1024 * 1024


@dataclass(frozen=True, slots=True)
class Limits:
    """What one backup may use. The defaults are those of D14 (and 16 MiB compressed)."""

    max_compressed: int = 16 * _MIB
    max_decompressed: int = 64 * _MIB
    max_depth: int = 16
    max_entries: int = 100_000
    max_string: int = 2048

    def to_dict(self) -> dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Limits:
        return cls(**{name: int(raw[name]) for name in cls.__slots__ if name in raw})


class TachibkRefusedError(ValueError):
    """A backup was refused. `reason` is a closed word and `code` the error code it answers with;
    never a part of the file."""

    def __init__(self, reason: str, code: str = INVALID) -> None:
        super().__init__(f"{code}: {reason}")
        self.reason = reason
        self.code = code


@dataclass(slots=True)
class RawChapter:
    url: str = ""
    read: bool = False  # the flag is exactly 1: any other value proves nothing
    last_page: int = 0
    number: float | None = None


@dataclass(slots=True)
class RawTracking:
    tracker: int = 0
    remote_id: int = 0
    last_chapter: float | None = None


@dataclass(slots=True)
class RawManga:
    source: int = 0
    url: str = ""
    title: str = ""
    favorite: bool = True  # the format's default when the field is absent
    chapters: list[RawChapter] = field(default_factory=list)
    tracking: list[RawTracking] = field(default_factory=list)


@dataclass(slots=True)
class RawBackup:
    schema_version: str
    manga: list[RawManga] = field(default_factory=list)
    source_ids: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RawBackup:
        try:
            return cls(
                schema_version=str(raw["schema_version"]),
                manga=[
                    RawManga(
                        source=int(m["source"]), url=str(m["url"]), title=str(m["title"]),
                        favorite=bool(m["favorite"]),
                        chapters=[
                            RawChapter(str(c["url"]), bool(c["read"]), int(c["last_page"]),
                                       None if c["number"] is None else float(c["number"]))
                            for c in m["chapters"]
                        ],
                        tracking=[
                            RawTracking(
                                int(t["tracker"]), int(t["remote_id"]),
                                None if t["last_chapter"] is None else float(t["last_chapter"]),
                            )
                            for t in m["tracking"]
                        ],
                    )
                    for m in raw["manga"]
                ],
                source_ids=[int(s) for s in raw["source_ids"]],
            )  # fmt: skip
        except (KeyError, TypeError, ValueError):
            raise TachibkRefusedError("malformed") from None


def gunzip(data: bytes, limits: Limits) -> bytes:
    """The one gzip member of the file, at most `max_decompressed` bytes."""
    if not data:
        raise TachibkRefusedError("empty")
    if len(data) > limits.max_compressed:
        raise TachibkRefusedError("too_large")
    if data[:2] != b"\x1f\x8b":
        raise TachibkRefusedError("not_gzip")
    stream = zlib.decompressobj(wbits=31)
    try:
        out = stream.decompress(data, limits.max_decompressed + 1)
    except zlib.error:
        raise TachibkRefusedError("malformed") from None
    if len(out) > limits.max_decompressed:
        raise TachibkRefusedError("decompressed_too_large")
    if not stream.eof or stream.unused_data:  # cut short, or a second member behind the first
        raise TachibkRefusedError("malformed")
    if not out:
        raise TachibkRefusedError("empty")
    return out


class _Reader:
    """The protobuf wire format over one buffer, counting every field it goes through."""

    __slots__ = ("budget", "buf", "limits")

    def __init__(self, buf: bytes, limits: Limits) -> None:
        self.buf, self.limits, self.budget = buf, limits, limits.max_entries

    def varint(self, pos: int, end: int) -> tuple[int, int]:
        result = shift = 0
        for _ in range(10):
            if pos >= end:
                raise TachibkRefusedError("malformed")
            byte = self.buf[pos]
            pos += 1
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result & 0xFFFFFFFFFFFFFFFF, pos
            shift += 7
        raise TachibkRefusedError("malformed")  # a varint is at most ten bytes

    def count(self) -> None:
        self.budget -= 1
        if self.budget < 0:
            raise TachibkRefusedError("too_many_entries")

    def fields(self, pos: int, end: int) -> Any:
        """Yield `(field, wire, value)` for each field of the message in `buf[pos:end]`: a varint
        value, the 4 or 8 raw bytes, or a `(start, stop)` slice for a length-delimited field. A
        group is skipped, with its depth bounded, and not yielded."""
        while pos < end:
            key, pos = self.varint(pos, end)
            number, wire = key >> 3, key & 7
            if number == 0:
                raise TachibkRefusedError("malformed")
            self.count()
            if wire == 0:
                value, pos = self.varint(pos, end)
                yield number, 0, value
            elif wire == 1 or wire == 5:
                size = 8 if wire == 1 else 4
                if end - pos < size:
                    raise TachibkRefusedError("malformed")
                yield number, wire, self.buf[pos : pos + size]
                pos += size
            elif wire == 2:
                length, pos = self.varint(pos, end)
                if length > end - pos:
                    raise TachibkRefusedError("malformed")
                yield number, 2, (pos, pos + length)
                pos += length
            elif wire == 3:
                pos = self._skip_group(number, pos, end)
            else:  # 4: an end of group nobody opened; 6 and 7 do not exist
                raise TachibkRefusedError("malformed")
        if pos != end:
            raise TachibkRefusedError("malformed")

    def _skip_group(self, number: int, pos: int, end: int) -> int:
        """Past the matching end tag, without recursion: the stack is a list bounded by depth."""
        open_groups = [number]
        while open_groups:
            if pos >= end:
                raise TachibkRefusedError("malformed")
            key, pos = self.varint(pos, end)
            inner, wire = key >> 3, key & 7
            if inner == 0:
                raise TachibkRefusedError("malformed")
            self.count()
            if wire == 3:
                open_groups.append(inner)
                if len(open_groups) > self.limits.max_depth:
                    raise TachibkRefusedError("too_deep")
            elif wire == 4:
                if open_groups.pop() != inner:
                    raise TachibkRefusedError("malformed")
            elif wire == 0:
                _, pos = self.varint(pos, end)
            elif wire in (1, 5):
                pos += 8 if wire == 1 else 4
                if pos > end:
                    raise TachibkRefusedError("malformed")
            elif wire == 2:
                length, pos = self.varint(pos, end)
                if length > end - pos:
                    raise TachibkRefusedError("malformed")
                pos += length
            else:
                raise TachibkRefusedError("malformed")
        return pos

    def text(self, span: tuple[int, int]) -> str:
        start, stop = span
        if stop - start > self.limits.max_string:
            raise TachibkRefusedError("string_too_long")
        try:
            return self.buf[start:stop].decode("utf-8")
        except UnicodeDecodeError:
            raise TachibkRefusedError("malformed") from None


def _signed(value: int) -> int:
    return value - (1 << 64) if value >= 1 << 63 else value


def _float(raw: Any) -> float | None:
    return float(struct.unpack("<f", raw)[0])


def _expect(wire: int, wanted: int) -> None:
    if wire != wanted:
        raise TachibkRefusedError("malformed")


def _chapter(reader: _Reader, span: tuple[int, int]) -> RawChapter:
    chapter = RawChapter()
    for number, wire, value in reader.fields(*span):
        if number == 1:
            _expect(wire, 2)
            chapter.url = reader.text(value)
        elif number == 4:
            _expect(wire, 0)
            chapter.read = value == 1
        elif number == 6:
            _expect(wire, 0)
            chapter.last_page = value if value < 1 << 31 else 0
        elif number == 9 and wire == 5:
            chapter.number = _float(value)
    return chapter


def _tracking(reader: _Reader, span: tuple[int, int]) -> RawTracking:
    entry, media = RawTracking(), None
    for number, wire, value in reader.fields(*span):
        if number == 1:
            _expect(wire, 0)
            entry.tracker = _signed(value)
        elif number in (3, 100):
            _expect(wire, 0)
            media = _signed(value) if number == 100 or media is None else media
        elif number == 6 and wire == 5:
            entry.last_chapter = _float(value)
    entry.remote_id = media or 0
    return entry


def _manga(reader: _Reader, span: tuple[int, int]) -> RawManga:
    manga = RawManga()
    for number, wire, value in reader.fields(*span):
        if number == 1:
            _expect(wire, 0)
            manga.source = _signed(value)
        elif number == 2:
            _expect(wire, 2)
            manga.url = reader.text(value)
        elif number == 3:
            _expect(wire, 2)
            manga.title = reader.text(value)
        elif number == 16:
            _expect(wire, 2)
            manga.chapters.append(_chapter(reader, value))
        elif number == 18:
            _expect(wire, 2)
            manga.tracking.append(_tracking(reader, value))
        elif number == 100:
            _expect(wire, 0)
            manga.favorite = value != 0
    return manga


def _source_id(reader: _Reader, span: tuple[int, int]) -> int | None:
    found = None
    for number, wire, value in reader.fields(*span):  # the name (field 1) is never read
        if number == 2:
            _expect(wire, 0)
            found = _signed(value)
    return found


def decode_backup(data: bytes, limits: Limits | None = None) -> RawBackup:
    """The series, chapters and tracker entries of a backup, or a `TachibkRefusedError`."""
    limits = limits or Limits()
    message = gunzip(data, limits)
    reader = _Reader(message, limits)
    top: set[int] = set()
    manga: list[RawManga] = []
    sources: list[int] = []
    for number, wire, value in reader.fields(0, len(message)):
        top.add(number)
        if number == 1:
            _expect(wire, 2)
            manga.append(_manga(reader, value))
        elif number == 100:
            _expect(wire, 2)
            source = _source_id(reader, value)
            if source is not None and source not in sources:
                sources.append(source)
        elif number in (2, 101, 104, 105, 106):
            _expect(wire, 2)  # skipped by its length: categories, preferences, repositories
    version = next((name for name, known in SCHEMA_VERSIONS.items() if top <= known), None)
    if version is None:
        raise TachibkRefusedError("unknown_schema", SCHEMA)
    return RawBackup(schema_version=version, manga=manga, source_ids=sources)
