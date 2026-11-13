"""Cover thumbnails: 160, 320 and 640 pixels, WebP, addressed by the cover's hash (EF-BIB-020).

The source is a cover the host itself stored under `/data/covers/<sha256>.webp` (already decoded
and re-encoded by the worker). A thumbnail is made on first use, by the decode worker and never by
this process (ENF-SEC-003), and kept under `/data/thumbnails/<aa>/<sha256>-<size>.webp`. The name
is computed from the hash and the size and from nothing else: the `thumbnail` row is a ledger of
what exists (for the catch-up and the collection), never a path that is trusted.

Nothing here knows who may see a cover: that is the caller's decision, taken before anything of
this module runs (the route asks the one reader filter; the jobs work on every referenced cover).

The collection deletes only inside the thumbnail folder, only names this module writes, and a link
is removed as a link. The cover files themselves are not its business.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import threading
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Final

import sqlalchemy as sa
import structlog

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.imaging import (
    ImageKind,
    ImageRejected,
    WorkerLimits,
    caps_for,
    make_thumbnail,
)
from shelfhold.infrastructure.schema import storage
from shelfhold.infrastructure.schema.storage import THUMBNAIL_SIZES
from shelfhold.infrastructure.secrets.atomic import write_private

__all__ = [
    "CACHE_CONTROL",
    "SIZES",
    "Collected",
    "Thumb",
    "ThumbnailService",
    "cover_hash",
    "etag_of",
]

log = structlog.get_logger(__name__)

SIZES: Final = THUMBNAIL_SIZES  # 160, 320, 640
STRAY_GRACE: Final = timedelta(days=1)  # a file with no row may be a generation in flight
MAX_THUMBNAIL_BYTES: Final = 8 * 1024 * 1024  # a thumbnail of 640 pixels is a few dozen KiB
CONCURRENT_WORKERS: Final = 2  # a library screen asks for sixty covers at once

MAX_AGE: Final = 86400  # a day: what a lowered ceiling may leave in a browser that had it
CACHE_CONTROL: Final = ", ".join(("private", f"max-age={MAX_AGE}", "immutable"))

_COVER_REF = re.compile(r"covers/([0-9a-f]{64})\.webp")
_HEX = re.compile(r"[0-9a-f]{64}")
_NAME = re.compile(r"([0-9a-f]{64})-(160|320|640)\.webp")
_SCRATCH = re.compile(r"\.[^/]*\.tmp")  # what `write_private` leaves when a run is cut


def cover_hash(ref: str) -> str | None:
    """The hex digest of a cover reference as the cover store writes it (`covers/<hex>.webp`),
    and nothing else: a path, an other extension, a longer or upper-case name is not one."""
    found = _COVER_REF.fullmatch(ref)
    return found.group(1) if found else None


def etag_of(digest: str, size: int) -> str:
    """The tag of one thumbnail: the same cover and size are the same bytes, for ever. It needs
    neither the file nor the worker, so a conditional request is answered without either."""
    return f'"{digest}-{size}"'


@dataclass(frozen=True, slots=True)
class Thumb:
    data: bytes
    etag: str
    size: int


@dataclass(frozen=True, slots=True)
class Collected:
    files: int = 0
    rows: int = 0


def _read_regular(path: Path, limit: int) -> bytes | None:
    """The bytes of a regular file under `limit`, never through a link; None when it is not that."""
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)  # noqa: TID251 (/data)
    except OSError:
        return None
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return None
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            return handle.read(limit + 1)
    finally:
        os.close(descriptor)


class ThumbnailService:
    def __init__(
        self,
        engine: sa.Engine,
        data_dir: Path,
        clock: Clock,
        *,
        limits: WorkerLimits | None = None,
    ) -> None:
        self._engine = engine
        self._covers = data_dir / "covers"
        self._folder = data_dir / "thumbnails"
        self._clock = clock
        self._limits = limits
        self._workers = threading.BoundedSemaphore(CONCURRENT_WORKERS)

    # -- names -----------------------------------------------------------------------------
    @staticmethod
    def _rel(digest: str, size: int) -> str:
        return f"{digest[:2]}/{digest}-{size}.webp"

    def _path(self, digest: str, size: int) -> Path:
        return self._folder / self._rel(digest, size)

    @staticmethod
    def valid(digest: str, size: int) -> bool:
        return _HEX.fullmatch(digest) is not None and size in SIZES

    # -- reading, lazily making ----------------------------------------------------------------
    def get(self, digest: str, size: int) -> Thumb | None:
        """The thumbnail, made now when it does not exist yet; None when the cover is not there,
        is not the one its name says, or is refused by the worker. Never a partial answer."""
        if not self.valid(digest, size):
            return None
        data = _read_regular(self._path(digest, size), MAX_THUMBNAIL_BYTES)
        if data is not None and self._known(digest, size):
            self._touch(digest, size)
            return Thumb(data, etag_of(digest, size), size)
        return self._make(digest, size)

    def _known(self, digest: str, size: int) -> bool:
        with self._engine.connect() as connection:
            return (
                connection.execute(
                    sa.select(sa.literal(1)).where(
                        storage.thumbnail.c.content_hash == f"sha256:{digest}",
                        storage.thumbnail.c.size == size,
                    )
                ).first()
                is not None
            )

    def _touch(self, digest: str, size: int) -> None:
        now, t = self._clock.now(), storage.thumbnail.c
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(storage.thumbnail)
                .where(
                    t.content_hash == f"sha256:{digest}",
                    t.size == size,
                    sa.or_(t.last_used_at.is_(None), t.last_used_at < now - timedelta(days=1)),
                )
                .values(last_used_at=now)
            )  # at most once a day: a screen of covers is not sixty writes

    def _source(self, digest: str) -> bytes | None:
        data = _read_regular(self._covers / f"{digest}.webp", caps_for(ImageKind.COVER).max_bytes)
        if data is None or hashlib.sha256(data).hexdigest() != digest:
            return None  # gone, a link, or not the bytes the name promises: never decoded
        return data

    def _make(self, digest: str, size: int) -> Thumb | None:
        source = self._source(digest)
        if source is None:
            return None
        try:
            with self._workers:
                made = make_thumbnail(
                    source, ImageKind.COVER, max_edge=size, fmt="webp", limits=self._limits
                )
        except ImageRejected as refused:
            log.info("thumbnail.refused", reason=refused.code)
            return None
        path = self._path(digest, size)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            write_private(path, made.data, overwrite=False)
        except FileExistsError:  # another request made it first: the same bytes, or a leftover
            on_disk = _read_regular(path, MAX_THUMBNAIL_BYTES)
            if on_disk is None:
                return None
            payload = on_disk
        else:
            payload = made.data
        now = self._clock.now()
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(storage.thumbnail)
                .prefix_with("OR IGNORE")
                .values(
                    content_hash=f"sha256:{digest}", size=size, path=self._rel(digest, size),
                    bytes=len(payload), created_at=now, last_used_at=now,
                )
            )  # fmt: skip
        return Thumb(payload, etag_of(digest, size), size)

    # -- the catch-up ----------------------------------------------------------------------------
    def _have(self, digest: str, size: int) -> bool:
        path = self._path(digest, size)
        return self._known(digest, size) and path.is_file() and not path.is_symlink()

    def catch_up(self, digests: Iterable[str], *, closing: Callable[[], bool]) -> int:
        """Make every missing size of the given covers; stops between two thumbnails when
        `closing()` says so. Returns how many it made."""
        made = 0
        for digest in sorted(set(digests)):
            for size in SIZES:
                if closing():
                    return made
                if not _HEX.fullmatch(digest) or self._have(digest, size):
                    continue
                made += self._make(digest, size) is not None
        return made

    # -- the collection ----------------------------------------------------------------------------
    def collect(self, referenced: Collection[str], *, closing: Callable[[], bool]) -> Collected:
        """Drop the thumbnails of covers no Edition points at, the rows whose file is gone and,
        after a grace, the files no row knows. Only inside the thumbnail folder."""
        files = rows = 0
        with self._engine.connect() as connection:
            ledger = [
                (str(r.content_hash).removeprefix("sha256:"), int(r.size))
                for r in connection.execute(
                    sa.select(storage.thumbnail.c.content_hash, storage.thumbnail.c.size)
                )
            ]
        known: set[tuple[str, int]] = set(ledger)
        for digest, size in ledger:
            if closing():
                return Collected(files, rows)
            orphan = digest not in referenced
            if orphan:
                files += self._remove(self._path(digest, size))
            elif self._path(digest, size).is_file() and not self._path(digest, size).is_symlink():
                continue
            self._forget(digest, size)
            rows += 1
        files += self._sweep(known, closing)
        return Collected(files, rows)

    def _forget(self, digest: str, size: int) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.delete(storage.thumbnail).where(
                    storage.thumbnail.c.content_hash == f"sha256:{digest}",
                    storage.thumbnail.c.size == size,
                )
            )

    @staticmethod
    def _remove(path: Path) -> int:
        """Unlink one name of ours (a link is removed as a link). 1 when something went."""
        try:
            path.unlink()
        except FileNotFoundError:
            return 0
        except IsADirectoryError:
            return 0  # never a folder
        return 1

    def _sweep(self, known: set[tuple[str, int]], closing: Callable[[], bool]) -> int:
        """Files of the folder with no row, older than the grace; interrupted writes too."""
        if not self._folder.is_dir():
            return 0
        cutoff = (self._clock.now() - STRAY_GRACE).timestamp()
        removed = 0
        for shard in sorted(self._folder.iterdir()):
            if shard.is_symlink() or not shard.is_dir():
                continue
            for entry in sorted(shard.iterdir()):
                if closing():
                    return removed
                named = _NAME.fullmatch(entry.name)
                ours = (
                    (named is not None and (named.group(1), int(named.group(2))) not in known)
                    or _SCRATCH.fullmatch(entry.name) is not None
                )  # fmt: skip
                if not ours or (entry.is_dir() and not entry.is_symlink()):
                    continue
                if entry.lstat().st_mtime < cutoff:
                    removed += self._remove(entry)
        return removed
