"""The chapters a block is made of: a CBZ on the disk, read for its pages and nothing else.

A path comes from the manifest, so it is not trusted: it must be relative, without `..`, `\\` or
NUL, and it is opened beneath the destination's root by `fsops` (no link followed, no climb), as a
regular file. A name inside
an archive is never used (the block writes its own names): only its extension is read, to tell a
page from anything else.

Pages are JPEG, PNG, GIF and WebP, the intersection of what Kavita counts and what KOReader's
MuPDF reads (ADR-0053 §2). An archive with a page in another image format (AVIF, JPEG XL, ...)
is left out whole, with a reason; entries that are not images (`ComicInfo.xml`) are omitted.
Sizes and CRC-32 come from the central directory.
"""

import hashlib
import os
import re
from dataclasses import dataclass
from typing import IO, Any, Final

from shelfhold.infrastructure.archives import (
    ArchiveLimits,
    SafeArchive,
    UnsafeArchiveError,
    safe_open_archive,
)
from shelfhold.infrastructure.fsops.confined import ConfinedRoot
from shelfhold.reading.bundle.limits import BundleLimits

PAGE_EXTENSIONS: Final = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
OTHER_IMAGES: Final = frozenset({"avif", "jxl", "heic", "heif", "jp2", "bmp", "tif", "tiff"})
_LIMITS: Final = ArchiveLimits(max_entries=5_000, max_entry_bytes=128 * 1024 * 1024)
_DIGITS = re.compile(r"(\d+)")
_CANONICAL = re.compile(r"[0-9]+\.[a-z0-9]+")


class ChapterLeftOutError(Exception):
    """The chapter is not in the block, for a stable reason: `format`, `missing`, `unsafe`,
    `unreadable`, `empty`."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ManifestMismatchError(Exception):
    """The file is there but is not the one the manifest describes."""


@dataclass(frozen=True, slots=True)
class Page:
    name: str  # as stored in the chapter's archive: read, never written into the block
    ext: str
    size: int
    crc: int


@dataclass(frozen=True)
class Scan:
    pages: tuple[Page, ...]  # in reading order
    canonical: bool  # flat, digits-and-extension names: the order Kavita would give
    digest: str  # sha256 of (ext, size, crc) of every page, in order: what a re-scan must equal


def clean_relative(rel: str) -> bool:
    return bool(rel) and not (
        rel.startswith("/") or "\\" in rel or "\0" in rel or ".." in rel.split("/") or "//" in rel
    )


def root_of(path_map: Any, root_subpath: str) -> str | None:
    """The folder the destination writes in: the first read-write entry of its path map (the first
    entry if none), plus its sub-path. None if the map or the sub-path cannot be trusted."""
    if not isinstance(path_map, list) or not path_map:
        return None
    entries = [e for e in path_map if isinstance(e, dict) and isinstance(e.get("local"), str)]
    chosen = next(
        (e for e in entries if e.get("mode", "rw") == "rw"), entries[0] if entries else None
    )
    if chosen is None:
        return None
    base = str(chosen["local"])
    if not base.startswith("/") or "\0" in base or ".." in base.split("/"):
        return None
    if root_subpath and not clean_relative(root_subpath):
        return None
    return base.rstrip("/") + ("/" + root_subpath if root_subpath else "")


def _natural(name: str) -> list[Any]:
    stem = name.rsplit(".", 1)[0].casefold()
    return [int(p) if p.isdigit() else p for p in _DIGITS.split(stem)]


class OpenSource:
    """An open chapter: the file descriptor stays for the whole transfer, so a replacement by
    `rename` leaves the old inode readable (ADR-0053 §8). The archive is audited by
    `safe_open_archive` (entry count, sizes, ratio, hidden data, names)."""

    def __init__(self, handle: IO[bytes], archive: SafeArchive, st: os.stat_result) -> None:
        self._handle, self._archive = handle, archive
        self.size, self.mtime_ns = st.st_size, st.st_mtime_ns

    def read_range(self, page: Page, start: int, length: int) -> bytes:
        """`length` bytes of one page from `start` (only the twelve samples of a digest use it)."""
        out = bytearray()
        with self._archive.open(page.name) as stream:
            skipped = 0
            while skipped < start:
                chunk = stream.read(min(65_536, start - skipped))
                if not chunk:
                    return bytes(out)
                skipped += len(chunk)
            out += stream.read(length) or b""
        return bytes(out)

    def page_reader(self, page: Page) -> IO[bytes]:
        return self._archive.open(page.name)  # type: ignore[return-value]

    def scan(self, limits: BundleLimits) -> Scan:
        kept: list[tuple[list[Any], int, Page]] = []
        for position, entry in enumerate(self._archive.entries):
            leaf = entry.name.rsplit("/", 1)[-1]
            if entry.is_dir or "." not in leaf:
                continue
            ext = leaf.rsplit(".", 1)[-1].lower()
            if ext in OTHER_IMAGES:
                raise ChapterLeftOutError("format")
            if ext not in PAGE_EXTENSIONS:
                continue  # `ComicInfo.xml` and the like: not a page
            if entry.encrypted or entry.size > limits.max_page_bytes:
                raise ChapterLeftOutError("unreadable")
            page = Page(entry.name, "jpg" if ext == "jpeg" else ext, entry.size, entry.crc32)
            kept.append((_natural(entry.name), position, page))
        if not kept:
            raise ChapterLeftOutError("empty")
        kept.sort(key=lambda row: (row[0], row[1]))
        pages = tuple(row[2] for row in kept)
        canonical = (
            all(_CANONICAL.fullmatch(p.name) for p in pages)
            and len({len(p.name.split(".")[0]) for p in pages}) == 1
        )
        digest = hashlib.sha256(
            "".join(f"{p.ext}:{p.size}:{p.crc}\n" for p in pages).encode()
        ).hexdigest()
        return Scan(pages, canonical, digest)

    def close(self) -> None:
        try:
            self._archive.close()
        finally:
            self._handle.close()


def open_source(root: str, rel: str) -> OpenSource:
    """Open one chapter's archive, or say why it is left out. Raises `ChapterLeftOutError`.

    The file is opened *beneath* the destination's root by `fsops` (a link, a `..` or a path that
    leaves the root is refused by the kernel call itself, not by a string check alone)."""
    if not clean_relative(rel):
        raise ChapterLeftOutError("unsafe")
    try:
        with ConfinedRoot.open(root) as confined:
            descriptor = confined.open_file(rel, os.O_RDONLY)
    except FileNotFoundError:
        raise ChapterLeftOutError("missing") from None
    except OSError:  # a link, a climb, not a regular file, a refused permission
        raise ChapterLeftOutError("unsafe") from None
    try:
        st = os.fstat(descriptor)
        handle = os.fdopen(descriptor, "rb", buffering=0)  # no stale buffer over a changed file
    except BaseException:
        os.close(descriptor)
        raise
    try:
        archive = safe_open_archive(handle, name="chapter.cbz", limits=_LIMITS)
    except (UnsafeArchiveError, OSError):
        handle.close()
        raise ChapterLeftOutError("unreadable") from None
    return OpenSource(handle, archive, st)
