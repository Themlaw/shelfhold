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
from shelfhold.infrastructure.volumes import erase_data_path, open_data_folder, write_data_file

__all__ = [
    "CACHE_CONTROL",
    "SIZES",
    "Collected",
    "Pending",
    "Thumb",
    "ThumbnailService",
    "cover_hash",
    "etag_of",
]

log = structlog.get_logger(__name__)

SIZES: Final = THUMBNAIL_SIZES  # 160, 320, 640
STRAY_GRACE: Final = timedelta(days=1)  # a file with no row may be a generation in flight
MAX_THUMBNAIL_BYTES: Final = 8 * 1024 * 1024  # a thumbnail of 640 pixels is a few dozen KiB
WAIT_SECONDS: Final = 0.25  # what a request waits for a worker slot or for the one decode
MAX_WAITERS: Final = 4  # threads that wait for one decode; the others answer at once
CONCURRENT_WORKERS: Final = 2  # a library screen asks for sixty covers at once

MAX_AGE: Final = 86400  # a day: what a lowered ceiling may leave in a browser that had it
CACHE_CONTROL: Final = ", ".join(("private", f"max-age={MAX_AGE}", "immutable"))

_COVER_REF = re.compile(r"covers/([0-9a-f]{64})\.webp")
_HEX = re.compile(r"[0-9a-f]{64}")
_SHARD = re.compile(r"[0-9a-f]{2}")
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


class Pending(Exception):
    """The thumbnail is being made and the wait for it is over: the caller answers with what it
    has (`ThumbnailService.fallback`), uncached, and the next request finds it done."""


class _Flight:
    """One generation in progress, and the requests that wait for it."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Thumb | None = None
        self.pending = False
        self.waiters = 0


class ThumbnailService:
    def __init__(
        self,
        engine: sa.Engine,
        data_dir: Path,
        clock: Clock,
        *,
        limits: WorkerLimits | None = None,
        wait: float = WAIT_SECONDS,
    ) -> None:
        self._engine = engine
        self._data = data_dir
        self._clock = clock
        self._limits = limits
        self._wait = wait
        self._workers = threading.BoundedSemaphore(CONCURRENT_WORKERS)
        self._lock = threading.Lock()
        self._flights: dict[tuple[str, int], _Flight] = {}

    # -- names -----------------------------------------------------------------------------
    @staticmethod
    def _rel(digest: str, size: int) -> str:
        return f"{digest[:2]}/{digest}-{size}.webp"

    @staticmethod
    def valid(digest: str, size: int) -> bool:
        return _HEX.fullmatch(digest) is not None and size in SIZES

    # -- the files: every step is a descriptor walk that follows no link (/data) -----------------
    def _read(self, folder: str, name: str, limit: int) -> bytes | None:
        """A regular file of `/data/<folder>`, never through a link (on the way or at the end),
        at most `limit` bytes; None when it is not there, is not that, or is bigger."""
        try:
            with open_data_folder(self._data, folder) as opened:
                data = opened.read(name, limit + 1)
        except (OSError, ValueError):
            return None
        return data if len(data) <= limit else None

    def _read_thumb(self, digest: str, size: int) -> bytes | None:
        name = f"{digest}-{size}.webp"
        return self._read(f"thumbnails/{digest[:2]}", name, MAX_THUMBNAIL_BYTES)

    # -- reading, lazily making ----------------------------------------------------------------
    def get(self, digest: str, size: int) -> Thumb | None:
        """The thumbnail, made now when it does not exist yet (once, however many ask; `Pending`
        when the wait for the one in progress is over); None when the cover is not there, is not
        the one its name says, or is refused by the worker. Never a partial answer."""
        if not self.valid(digest, size):
            return None
        if self._known(digest, size):
            data = self._read_thumb(digest, size)
            if data is not None:
                self._touch(digest, size)
                return Thumb(data, etag_of(digest, size), size)
        return self._single_flight(digest, size)

    def fallback(self, digest: str) -> bytes | None:
        """What a request that gave up waiting is sent: the cover itself, already reduced (the
        host stores it at 1200 pixels at most) and verified, to be shown and never cached."""
        return self._source(digest)

    def _single_flight(self, digest: str, size: int) -> Thumb | None:
        key = (digest, size)
        with self._lock:
            flight = self._flights.get(key)
            leader = flight is None
            if flight is None:
                flight = self._flights[key] = _Flight()
            elif flight.waiters >= MAX_WAITERS:  # enough threads wait on this one already
                raise Pending
            else:
                flight.waiters += 1
        if not leader:
            if not flight.done.wait(self._wait) or flight.pending:
                raise Pending
            return flight.result
        try:
            flight.result = self._make(digest, size, wait=self._wait)
        except Pending:
            flight.pending = True
            raise
        finally:
            with self._lock:
                del self._flights[key]
            flight.done.set()
        return flight.result

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
        data = self._read("covers", f"{digest}.webp", caps_for(ImageKind.COVER).max_bytes)
        if data is None or hashlib.sha256(data).hexdigest() != digest:
            return None  # gone, a link, or not the bytes the name promises: never decoded
        return data

    def _make(self, digest: str, size: int, *, wait: float | None = None) -> Thumb | None:
        """Decode in the worker and keep the result. `wait` bounds the queue for a worker slot
        (`Pending` past it); None is the job's: as long as it takes. What is on disk at the name
        is replaced, never served: only the bytes made here leave."""
        source = self._source(digest)
        if source is None:
            return None
        if not self._workers.acquire(timeout=wait):
            raise Pending
        try:
            made = make_thumbnail(
                source, ImageKind.COVER, max_edge=size, fmt="webp", limits=self._limits
            )
        except ImageRejected as refused:
            log.info("thumbnail.refused", reason=refused.code)
            return None
        finally:
            self._workers.release()
        try:
            write_data_file(self._data, f"thumbnails/{self._rel(digest, size)}", made.data)
        except (OSError, ValueError):  # a link on the way, or the disk: nothing is kept
            log.info("thumbnail.not_kept")
            return None
        now = self._clock.now()
        with self._engine.begin() as connection:
            connection.execute(
                sa.insert(storage.thumbnail)
                .prefix_with("OR REPLACE")
                .values(
                    content_hash=f"sha256:{digest}", size=size, path=self._rel(digest, size),
                    bytes=len(made.data), created_at=now, last_used_at=now,
                )
            )  # fmt: skip
        return Thumb(made.data, etag_of(digest, size), size)

    # -- the catch-up ----------------------------------------------------------------------------
    def _have(self, digest: str, size: int) -> bool:
        return self._known(digest, size) and self._have_file(digest, size)

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
            if digest not in referenced:
                files += self._remove(digest[:2], f"{digest}-{size}.webp")
            elif self._have_file(digest, size):
                continue
            self._forget(digest, size)
            rows += 1
        files += self._sweep(known, closing)
        return Collected(files, rows)

    def _have_file(self, digest: str, size: int) -> bool:
        try:
            with open_data_folder(self._data, f"thumbnails/{digest[:2]}") as folder:
                return folder.is_regular(f"{digest}-{size}.webp")
        except (OSError, ValueError):
            return False

    def _forget(self, digest: str, size: int) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.delete(storage.thumbnail).where(
                    storage.thumbnail.c.content_hash == f"sha256:{digest}",
                    storage.thumbnail.c.size == size,
                )
            )

    def _remove(self, shard: str, name: str) -> int:
        """Remove one name of ours from its shard, by `erase_data_path` (the house rule for
        /data: a descriptor walk, a link on the way refused, a link at the end removed as a link).
        Never a folder. 1 when something went."""
        try:
            with open_data_folder(self._data, f"thumbnails/{shard}") as folder:
                info = folder.stat_of(name)
            if info is None or stat.S_ISDIR(info.st_mode):
                return 0
            return int(erase_data_path(self._data, f"thumbnails/{shard}/{name}"))
        except (OSError, ValueError):
            return 0  # a linked shard, a folder in the way: left as it is

    def _sweep(self, known: set[tuple[str, int]], closing: Callable[[], bool]) -> int:
        """Files of the folder with no row, older than the grace; interrupted writes too."""
        cutoff = (self._clock.now() - STRAY_GRACE).timestamp()
        removed = 0
        try:
            with open_data_folder(self._data, "thumbnails") as root:
                shards = sorted(n for n in root.names() if _SHARD.fullmatch(n))
        except (OSError, ValueError):
            return 0
        for shard in shards:
            try:
                with open_data_folder(self._data, f"thumbnails/{shard}") as folder:
                    entries = [(n, folder.stat_of(n)) for n in sorted(folder.names())]
            except (OSError, ValueError):
                continue  # a link or a file where a shard should be: not ours to enter
            for name, info in entries:
                if closing():
                    return removed
                named = _NAME.fullmatch(name)
                ours = (
                    (named is not None and (named.group(1), int(named.group(2))) not in known)
                    or _SCRATCH.fullmatch(name) is not None
                )  # fmt: skip
                if ours and info is not None and info.st_mtime < cutoff:
                    removed += self._remove(shard, name)
        return removed
