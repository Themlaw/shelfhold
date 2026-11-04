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
import itertools
import stat
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path, PurePath
from types import TracebackType
from typing import BinaryIO, Final, Self

_ZIP_MAGICS: Final = (b"PK\x03\x04", b"PK\x05\x06")  # a local header, or an empty archive
_OTHER_MAGICS: Final = {b"Rar!\x1a\x07": "rar", b"7z\xbc\xaf\x27\x1c": "7z"}
_EXTENSIONS: Final = {
    ".cbz": "zip", ".zip": "zip", ".epub": "zip",
    ".cbr": "rar", ".rar": "rar", ".cb7": "7z", ".7z": "7z", ".cbt": "tar", ".tar": "tar",
}  # fmt: skip
_FIRST_LOCAL_HEADER: Final = 30  # bytes of a local file header before the name
_CHUNK: Final = 64 * 1024
_MAX_NAME_BYTES: Final = 1024
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
        if not self.closed:
            self._inner.close()
        super().close()


class SafeArchive:
    """An audited ZIP container. Use as a context manager; never extracts."""

    def __init__(
        self, source: BinaryIO, owned: bool, zf: zipfile.ZipFile, limits: ArchiveLimits
    ) -> None:
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
        self._audit_overlap(infos)
        self._by_name = {i.filename: i for i in infos}
        self.entries = tuple(
            ArchiveEntry(i.filename, i.file_size, i.compress_size, i.filename.endswith("/"))
            for i in infos
        )

    def _ratio_exceeded(self, size: int, packed: int) -> bool:
        if size <= self.limits.ratio_floor_bytes:
            return False
        return packed * self.limits.max_ratio < size

    @staticmethod
    def _audit_overlap(infos: list[zipfile.ZipInfo]) -> None:
        ordered = sorted(infos, key=lambda i: i.header_offset)
        for current, following in itertools.pairwise(ordered):
            lowest_end = current.header_offset + _FIRST_LOCAL_HEADER + current.compress_size
            if lowest_end > following.header_offset:
                raise UnsafeArchiveError(
                    "overlap", "entries share bytes", entry=ascii(following.filename)
                )

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

    def read(self, name: str, max_bytes: int | None = None) -> bytes:
        """The whole entry, refused if it exceeds `max_bytes` (default: the per-entry cap)."""
        cap = self.limits.max_entry_bytes if max_bytes is None else max_bytes
        info = self._info(name)
        if info.file_size > cap:
            raise UnsafeArchiveError("entry_too_large", f"{info.file_size} > {cap}", entry=name)
        with self.open(name) as stream:
            return stream.read()

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
        try:
            zf = zipfile.ZipFile(stream)
        except (zipfile.BadZipFile, NotImplementedError, OSError, EOFError) as error:
            raise UnsafeArchiveError("corrupt", str(error)) from None
        archive = SafeArchive(stream, owned, zf, limits)
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
