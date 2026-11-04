"""safe_open_archive: the single archive entry point (ENF-SEC-002).

Archives come from outside (a connected service, an import folder). They are read as streams, one
entry at a time, and never extracted: nothing here writes to the file system, and no entry name is
ever used as a path. Before any byte is decompressed the central directory is audited against
ArchiveLimits: entry count, declared size per entry and in total, compression ratio, overlapping
entries (the overlap bomb), encrypted, symbolic-link and special-file entries, duplicate names
(exact, case-insensitive and Unicode-equivalent) and unsafe names (parent directories, absolute
and Windows paths, control, format and bidirectional characters). Reading is then capped again on
the bytes actually produced, because a header can lie. The container format is decided by its
magic bytes and must agree with the file extension. Only ZIP containers (cbz, zip, epub) are
supported in M1a; anything else is refused until a lot adds it behind this same function.
"""

from __future__ import annotations

import io
import stat
import struct
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePath
from types import TracebackType
from typing import IO, BinaryIO, Final, Self

_ZIP_MAGICS: Final = (b"PK\x03\x04", b"PK\x05\x06")  # a local header, or an empty archive
_OTHER_MAGICS: Final = {b"Rar!\x1a\x07": "rar", b"7z\xbc\xaf\x27\x1c": "7z"}
_EXTENSIONS: Final = {
    ".cbz": "zip", ".zip": "zip", ".epub": "zip",
    ".cbr": "rar", ".rar": "rar", ".cb7": "7z", ".7z": "7z", ".cbt": "tar", ".tar": "tar",
}  # fmt: skip
_FIRST_LOCAL_HEADER: Final = 30  # bytes of a local file header before the name
_CHUNK: Final = 64 * 1024
_MAX_NAME_BYTES: Final = 1024
_CD_RECORD_BYTES: Final = 46  # fixed part of a central directory record
_CD_EXTRA_MARGIN: Final = 1024  # extra field and comment allowance per record
_EOCD_BYTES: Final = 22
_EOCD_SEARCH: Final = _EOCD_BYTES + 0xFFFF + 20  # comment is at most 64 KiB; plus the ZIP64 locator
_DESCRIPTOR_GAPS: Final = frozenset({12, 16, 20, 24})  # data descriptor with/without signature
_FORBIDDEN_CATEGORIES: Final = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


class UnsafeArchiveError(ValueError):
    """The archive or one of its entries is refused. `reason` is a stable word."""

    def __init__(self, reason: str, message: str, *, entry: str | None = None) -> None:
        super().__init__(f"{reason}: {message}")
        self.reason = reason
        self.entry = entry


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    max_entries: int = 10_000
    max_entry_bytes: int = 128 * 1024 * 1024
    max_total_bytes: int = 4 * 1024 * 1024 * 1024
    max_ratio: int = 100  # decompressed / compressed, per entry and for the whole archive
    ratio_floor_bytes: int = 1024 * 1024  # below this size the ratio is not a signal
    max_buffered_bytes: int = 32 * 1024 * 1024  # default cap of read(); streaming has its own
    max_read_bytes: int | None = None  # bytes produced across all reads; None = max_total_bytes

    @property
    def read_cap(self) -> int:
        return self.max_total_bytes if self.max_read_bytes is None else self.max_read_bytes


DEFAULT_LIMITS: Final = ArchiveLimits()


@dataclass(frozen=True, slots=True)
class ArchiveEntry:
    name: str
    size: int
    compressed_size: int
    is_dir: bool


def _check_name(name: str) -> None:
    """`name` is the name as stored (ZipInfo.orig_filename), before zipfile truncates at a NUL."""

    def bad(why: str) -> UnsafeArchiveError:
        return UnsafeArchiveError("unsafe_path", why, entry=ascii(name))

    if not name or len(name.encode("utf-8", "surrogatepass")) > _MAX_NAME_BYTES:
        raise bad("empty or overlong name")
    if "\\" in name:
        raise bad("backslash")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":" and name[0].isalpha()):
        raise bad("absolute or drive path")
    if ".." in name.split("/"):
        raise bad("parent directory")
    if any(unicodedata.category(ch) in _FORBIDDEN_CATEGORIES for ch in name):
        raise bad("control, format or bidirectional character")


def _dedup_key(name: str) -> str:
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", name).casefold()).rstrip("/")


def _scan_directory(stream: BinaryIO, limits: ArchiveLimits) -> tuple[int, int]:
    """Read the end-of-central-directory records by hand, before zipfile loads the directory.

    zipfile builds one ZipInfo per record before we can look at any of them, so the entry cap and
    the directory size cap are applied here, from the EOCD (and ZIP64 EOCD) fields, to a file
    that costs at most 64 KiB of reading. Returns (central directory offset, entry count).
    """
    size = stream.seek(0, io.SEEK_END)
    start = max(0, size - _EOCD_SEARCH)
    stream.seek(start)
    tail = stream.read(size - start)
    found = tail.rfind(b"PK\x05\x06")  # the last one, as zipfile does
    if found < 0 or len(tail) - found < _EOCD_BYTES:
        raise UnsafeArchiveError("corrupt", "no end of central directory")
    eocd_at = start + found
    fields = struct.unpack("<4s4H2LH", tail[found : found + _EOCD_BYTES])
    total, cd_size, cd_offset = fields[4:7]
    directory_end = eocd_at
    if total == 0xFFFF or 0xFFFFFFFF in (cd_size, cd_offset):
        stream.seek(max(0, eocd_at - 20))
        locator = stream.read(20) if eocd_at >= 20 else b""
        if locator[:4] == b"PK\x06\x07":
            record_at = struct.unpack("<4sLQL", locator)[2]
            stream.seek(record_at)
            record = stream.read(56)
            if len(record) < 56 or record[:4] != b"PK\x06\x06":
                raise UnsafeArchiveError("corrupt", "bad ZIP64 end of central directory")
            total, cd_size, cd_offset = struct.unpack("<3Q", record[32:56])
            directory_end = record_at
    per_record = _CD_RECORD_BYTES + _MAX_NAME_BYTES + _CD_EXTRA_MARGIN
    if total > limits.max_entries or cd_size > limits.max_entries * per_record:
        raise UnsafeArchiveError(
            "too_many_entries", f"{total} entries, directory of {cd_size} bytes"
        )
    if cd_offset + cd_size != directory_end:
        raise UnsafeArchiveError("hidden_data", "bytes between the entries and the directory end")
    return cd_offset, total


def _detect(head: bytes) -> str | None:
    if head.startswith(_ZIP_MAGICS):
        return "zip"
    for magic, kind in _OTHER_MAGICS.items():
        if head.startswith(magic):
            return kind
    return None


class EntryStream(io.RawIOBase):
    """A bounded, counting view of one entry: decompresses on demand, never to disk."""

    def __init__(self, archive: SafeArchive, info: zipfile.ZipInfo) -> None:
        super().__init__()
        self._inner: IO[bytes] | None = None  # set first: close() runs even if opening fails
        self._archive = archive
        self._name = info.filename
        self._left = info.file_size
        try:
            self._inner = archive._zip.open(info)
        except (zipfile.BadZipFile, NotImplementedError, RuntimeError, zlib.error) as error:
            raise UnsafeArchiveError("corrupt", str(error), entry=ascii(self._name)) from None

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        want = min(
            len(buffer), self._left + 1
        )  # one byte more than declared exposes a lying header
        assert self._inner is not None  # noqa: S101
        try:
            chunk = self._inner.read(want)
        except (zipfile.BadZipFile, EOFError, zlib.error) as error:
            raise UnsafeArchiveError("corrupt", str(error), entry=ascii(self._name)) from None
        if len(chunk) > self._left:
            raise UnsafeArchiveError(
                "entry_too_large", "more data than declared", entry=ascii(self._name)
            )
        self._left -= len(chunk)
        self._archive._account(len(chunk), self._name)
        buffer[: len(chunk)] = chunk
        return len(chunk)

    def close(self) -> None:
        if not self.closed and self._inner is not None:
            self._inner.close()
        super().close()


class SafeArchive:
    """An audited ZIP container. Use as a context manager; never extracts."""

    def __init__(
        self,
        source: BinaryIO,
        owned: bool,
        zf: zipfile.ZipFile,
        limits: ArchiveLimits,
        directory_offset: int,
    ) -> None:
        self._directory_offset = directory_offset
        self._source = source
        self._owned = owned
        self._zip = zf
        self.limits = limits
        self._by_name: dict[str, zipfile.ZipInfo] = {}
        self._produced = 0
        self.entries: tuple[ArchiveEntry, ...] = ()

    # -- audit ------------------------------------------------------------------------------
    def _audit(self) -> None:
        limits = self.limits
        infos = self._zip.infolist()
        if len(infos) > limits.max_entries:
            raise UnsafeArchiveError("too_many_entries", f"{len(infos)} > {limits.max_entries}")
        seen: set[str] = set()
        total = packed = 0
        for info in infos:
            name = info.filename
            _check_name(info.orig_filename)
            _check_name(name)
            key = _dedup_key(name)
            if key in seen:
                raise UnsafeArchiveError("duplicate_name", "same name twice", entry=ascii(name))
            seen.add(key)
            if info.flag_bits & 0x41:  # encrypted, or strong encryption
                raise UnsafeArchiveError("encrypted", "encrypted entry", entry=ascii(name))
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise UnsafeArchiveError("symlink", "symbolic link", entry=ascii(name))
            is_dir_name = name.endswith("/")
            if is_dir_name and (info.file_size != 0 or stat.S_IFMT(mode) not in (0, stat.S_IFDIR)):
                raise UnsafeArchiveError(
                    "unsafe_directory", "directory with data", entry=ascii(name)
                )
            if stat.S_ISDIR(mode) and not is_dir_name:
                raise UnsafeArchiveError(
                    "unsafe_directory", "file marked as directory", entry=ascii(name)
                )
            if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR):
                raise UnsafeArchiveError("special_file", "device or fifo", entry=ascii(name))
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise UnsafeArchiveError(
                    "unsupported_compression", f"method {info.compress_type}", entry=ascii(name)
                )
            if info.file_size > limits.max_entry_bytes:
                raise UnsafeArchiveError("entry_too_large", f"{info.file_size}", entry=ascii(name))
            if self._ratio_exceeded(info.file_size, info.compress_size):
                raise UnsafeArchiveError("ratio", "compression ratio", entry=ascii(name))
            total += info.file_size
            packed += info.compress_size
            if total > limits.max_total_bytes:
                raise UnsafeArchiveError("total_too_large", f"{total} declared")
        if self._ratio_exceeded(total, packed):
            raise UnsafeArchiveError("ratio", "compression ratio of the whole archive")
        self._audit_layout(infos)
        self._by_name = {i.filename: i for i in infos}
        self.entries = tuple(
            ArchiveEntry(i.filename, i.file_size, i.compress_size, i.filename.endswith("/"))
            for i in infos
        )

    def _ratio_exceeded(self, size: int, packed: int) -> bool:
        if size <= self.limits.ratio_floor_bytes:
            return False
        return packed * self.limits.max_ratio < size

    def _audit_layout(self, infos: list[zipfile.ZipInfo]) -> None:
        """Local records and their data must tile the file from byte 0 to the directory.

        A local header the directory does not list is an entry that readers which walk the file
        (streaming extractors) would see and zipfile would not; two records over the same bytes
        is an overlap bomb. Neither gap nor surplus is tolerated, except a data descriptor.
        """
        position = 0
        descriptor = False  # the previous entry is followed by a data descriptor

        def check_gap(until: int, entry: str | None) -> None:
            gap = until - position
            if gap < 0:
                raise UnsafeArchiveError("overlap", "entries share bytes", entry=entry)
            if gap and not (descriptor and gap in _DESCRIPTOR_GAPS):
                raise UnsafeArchiveError(
                    "hidden_data", "bytes outside any listed entry", entry=entry
                )

        for info in sorted(infos, key=lambda i: i.header_offset):
            check_gap(info.header_offset, ascii(info.filename))
            self._source.seek(info.header_offset)
            local = self._source.read(_FIRST_LOCAL_HEADER)
            if len(local) < _FIRST_LOCAL_HEADER or local[:4] != b"PK\x03\x04":
                raise UnsafeArchiveError("corrupt", "bad local header", entry=ascii(info.filename))
            name_len, extra_len = struct.unpack("<2H", local[26:30])
            position = info.header_offset + _FIRST_LOCAL_HEADER + name_len + extra_len
            position += info.compress_size
            descriptor = bool(struct.unpack("<H", local[6:8])[0] & 0x08)
        check_gap(self._directory_offset, None)

    # -- reading ----------------------------------------------------------------------------
    def _account(self, produced: int, name: str) -> None:
        self._produced += produced
        if self._produced > self.limits.read_cap:
            raise UnsafeArchiveError("total_too_large", "read cap reached", entry=ascii(name))

    def _info(self, name: str) -> zipfile.ZipInfo:
        try:
            return self._by_name[name]
        except KeyError:
            raise KeyError(name) from None

    def open(self, name: str) -> EntryStream:
        """A bounded stream over one entry (KeyError if there is none)."""
        return EntryStream(self, self._info(name))

    def read(self, name: str, max_bytes: int | None = None) -> bytearray:
        """The whole entry in one buffer sized from the header (no second copy).

        Refused if it exceeds `max_bytes` (default: ArchiveLimits.max_buffered_bytes); use open()
        to stream anything larger.
        """
        cap = self.limits.max_buffered_bytes if max_bytes is None else max_bytes
        info = self._info(name)
        if info.file_size > cap:
            raise UnsafeArchiveError("entry_too_large", f"{info.file_size} > {cap}", entry=name)
        buffer = bytearray(info.file_size)
        view = memoryview(buffer)
        filled = 0
        with self.open(name) as stream:
            while filled < len(buffer):
                got = stream.readinto(view[filled:])
                if not got:
                    raise UnsafeArchiveError("corrupt", "entry shorter than declared", entry=name)
                filled += got
            if stream.read(1):  # the header understated the size
                raise UnsafeArchiveError("entry_too_large", "more data than declared", entry=name)
        return buffer

    def read_head(self, name: str, size: int) -> bytes:
        """The first `size` bytes of an entry, for magic-byte checks."""
        with self.open(name) as stream:
            return stream.read(size)

    # -- lifecycle --------------------------------------------------------------------------
    def close(self) -> None:
        self._zip.close()
        if self._owned:
            self._source.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def safe_open_archive(
    source: Path | str | BinaryIO,
    *,
    name: str | None = None,
    limits: ArchiveLimits = DEFAULT_LIMITS,
) -> SafeArchive:
    """Audit and open an archive. `name` gives the extension when `source` is a stream."""
    owned = isinstance(source, str | PurePath)
    stream: BinaryIO = open(source, "rb") if isinstance(source, str | PurePath) else source  # noqa: SIM115
    declared = name if name is not None else (str(source) if owned else None)
    try:
        stream.seek(0)
        head = stream.read(8)
        stream.seek(0)
        found = _detect(head)
        expected = _EXTENSIONS.get(PurePath(declared).suffix.lower()) if declared else None
        if declared and expected is None:
            raise UnsafeArchiveError(
                "unsupported_format", f"extension of {PurePath(declared).name!r}"
            )
        if found != (expected or found) or found is None:
            raise UnsafeArchiveError("magic_mismatch", "extension and magic bytes disagree")
        if found != "zip":
            raise UnsafeArchiveError("unsupported_format", f"{found} archives are not supported")
        directory_offset, _ = _scan_directory(stream, limits)
        stream.seek(0)
        try:
            zf = zipfile.ZipFile(stream)
        except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError) as error:
            raise UnsafeArchiveError("corrupt", str(error)) from None
        archive = SafeArchive(stream, owned, zf, limits, directory_offset)
        try:
            archive._audit()
        except BaseException:
            zf.close()
            raise
        return archive
    except BaseException:
        if owned:
            stream.close()
        raise
