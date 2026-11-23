"""Image renditions: a client without a format is served a JPEG or a PNG made from it (EF-BIB-015).

The bytes are untrusted (ENF-SEC-003). In this order, and nothing is written before the last step:
1. the `content` is a known one, and a processor that sends bytes out of the host is never given a
   `watermarked` file (the flag is read here from the row, never taken from the caller);
2. the answer may already be there: a row and a file whose hash is the one the row says;
3. the source is bounded **before** a worker starts, from its first bytes: its size, its format
   (AVIF or WebP, by magic bytes; JPEG XL has no decoder, ADR-0054) and the pixels its header
   announces (`image.too_large` at stage `header`). The worker then checks them again, with its
   memory, CPU and time limits and no network, before it decodes a pixel;
4. the converted bytes are written under `/data/cache/renditions/<aa>/<hex>.<kind>`, named by
   their own hash, then the row. A failure at any step leaves neither.

A rendition is derived from a `content` and goes with it: `purge --now` erases the files, then the
rows (`rendition_rels` of the purge inventory, ADR-0048; `collect_orphans` sweeps what a race
leaves). The source is not
modified, a `watermarked` file included: the converted file is a separate one that stays at home.
Nothing here knows who may see a content: that is the caller's decision, taken first.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import sqlalchemy as sa
import structlog

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.imaging import (
    ImageCaps,
    ImageRejected,
    Reencoded,
    caps_for,
    reencode_image,
)
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.infrastructure.volumes import (
    DataFolder,
    erase_data_path,
    open_data_folder,
    write_data_file,
)
from shelfhold.library.application.kepub import KepubError, kepubify
from shelfhold.library.domain.content_hash import FILE_HASH_BY
from shelfhold.library.domain.failures import TombstonedContentError
from shelfhold.library.domain.rendition import (
    ALL_KIND_MEDIA,
    DECODABLE_SOURCES,
    KEPUB_KIND,
    LOCAL_PROCESSOR,
    Processor,
    RenditionParams,
    UnsupportedProcessorError,
    UnsupportedSourceError,
    avif_announced_size,
    ensure_processor_allowed,
    sniff_source,
    webp_announced_size,
)
from shelfhold.library.tombstones.values import EDITION_KEY_COLUMNS, ContentHash, PlaceRef

__all__ = [
    "CACHE_DIR",
    "STRAY_GRACE",
    "Converter",
    "Rendition",
    "RenditionBusyError",
    "RenditionService",
    "UnknownSourceError",
    "collect_orphans",
]

log = structlog.get_logger(__name__)

CACHE_DIR: Final = "cache/renditions"  # under /data
MAX_RENDITION_BYTES: Final = 128 * 1024 * 1024  # the byte cap of a page: what may be read back
Converter = Callable[[bytes, RenditionParams, ImageCaps], Reencoded]
STRAY_GRACE: Final = timedelta(minutes=10)  # a render writes its file, then its row: seconds
# The access of a rendition is recorded on its file at most once a day (a screen of pages is not
# a write each): that time is the LRU key of the retention sweep (L12, EF-BIB-018).
TOUCH_GRACE: Final = timedelta(days=1)
# The renditions an in-flight request is reading right now (content hash -> how many), shared by
# every service of the process: the reader serves, the sweep of the quotas asks `pinned` and
# leaves a file a ticket still needs (L12: a derivative being served is never evicted).
_PIN_LOCK: Final = threading.Lock()
_PINNED: dict[str, int] = {}
_SHARD: Final = re.compile(r"[0-9a-f]{2}")
_NAME: Final = re.compile(r"([0-9a-f]{64})\.(jpeg|png|kepub)")
KEPUB_PARAMS: Final = RenditionParams(kind=KEPUB_KIND)
MAX_KEPUB_BYTES: Final = 64 * 1024 * 1024  # an EPUB handed to the pure-Python conversion
# Threads a request may hold in the cache (waiting for a conversion or making one), in all and per
# profile; conversions that run at once; how long one waits for a conversion slot (seconds).
MAX_HELD: Final = 8
MAX_PER_OWNER: Final = 2
MAX_CONVERTING: Final = 2
WAIT_S: Final = 1.0
RETRY_AFTER_S: Final = 5
_FORMATS: Final = ("WEBP", "AVIF")  # what the worker is allowed to open for a rendition


class SourceTombstones(Protocol):
    """The part of the tombstone service a render needs. A purge records its tombstones before
    it deletes anything (ADR-0048): the `place` of each Unit it takes, and the hashes of its
    files. Only the place is the content's own: another Unit that holds the same bytes is not
    covered by it, and is not refused (its rendition is made again)."""

    def has_values(self, candidate: PlaceRef) -> bool: ...

    def check_content(self, hashes: Iterable[ContentHash] = ()) -> bool: ...


@dataclass(frozen=True, slots=True)
class _Origin:
    """What a content says of itself: the hash of its file, the flag, and its own `place`
    (None for a file that carries several Units)."""

    source: str
    watermarked: bool
    place: PlaceRef | None


class RenditionBusyError(RuntimeError):
    """Every thread the cache lends is held (in all, or by this profile): come back in a moment.
    Said before the cache is looked in, so that it is no oracle on what another profile read."""

    def __init__(self, retry_after_s: int = RETRY_AFTER_S) -> None:
        super().__init__("rendition busy")
        self.retry_after_s = retry_after_s


class UnknownSourceError(LookupError):
    """No `content` has this identifier: nothing is made for a source that is not one."""


@dataclass(frozen=True, slots=True)
class Rendition:
    source_content_hash: str
    kind: str
    params: str  # canonical JSON
    content_hash: str  # of the converted bytes
    size: int

    @property
    def media_type(self) -> str:
        return ALL_KIND_MEDIA[self.kind]

    @property
    def rel(self) -> str:
        """The file, relative to `/data`: the name is the hash and nothing else."""
        return _rel(self.content_hash, self.kind)


def _text(canonical: str) -> sa.ColumnElement[str]:
    """The canonical JSON as the text it is: the `JSON` column type would encode it a second
    time, and the key is compared byte for byte."""
    return sa.type_coerce(canonical, sa.Text())


def _hex(prefixed: str) -> str:
    return prefixed.removeprefix("sha256:")


def _rel(content_hash: str, kind: str) -> str:
    digest = _hex(content_hash)
    return f"{CACHE_DIR}/{digest[:2]}/{digest}.{kind}"


def convert_in_worker(data: bytes, params: RenditionParams, caps: ImageCaps) -> Reencoded:
    """The default converter: the bounded, network-less decode worker, with the caps of this ask
    (lowered by the caller, WebP and AVIF only): the worker checks them on the header again."""
    return reencode_image(data, fmt=params.kind, quality=params.quality, caps=caps)


def _check_source(data: bytes, *, max_bytes: int, max_pixels: int) -> None:
    """Everything that can be said about the bytes without decoding a pixel."""
    if len(data) > max_bytes:
        raise ImageRejected("image.too_large", "bytes", "input")
    found = sniff_source(data[:64])
    if found not in DECODABLE_SOURCES:  # JPEG XL has no decoder; anything else is not a source
        raise UnsupportedSourceError(found or "unknown")
    if found == "avif":
        announced = avif_announced_size(data)  # the whole file is in hand: boxes are walked by size
        if announced is None:  # no `meta`, no `ispe`, a malformed box: not a header to trust
            raise ImageRejected("image.format_refused", "avif header", "header")
    else:
        announced = webp_announced_size(data)  # every chunk is walked, the largest size counts
        if announced is None:
            raise ImageRejected("image.format_refused", "webp header", "header")
    if announced[0] * announced[1] > max_pixels:
        raise ImageRejected("image.too_large", "pixels", "header")


class RenditionService:
    def __init__(
        self,
        engine: sa.Engine,
        data_dir: Path,
        clock: Clock,
        *,
        tombstones: SourceTombstones,
        convert: Converter | None = None,
        kepub: Callable[[bytes], bytes] | None = None,
        max_held: int = MAX_HELD,
        max_per_owner: int = MAX_PER_OWNER,
        max_converting: int = MAX_CONVERTING,
        wait_s: float = WAIT_S,
        max_kepub_bytes: int = MAX_KEPUB_BYTES,
    ) -> None:
        self._tombstones = tombstones
        self._kepub = kepub or kepubify
        self._max_held, self._max_per_owner = max_held, max_per_owner
        self._max_kepub_bytes, self._wait_s = max_kepub_bytes, wait_s
        self._converting = threading.BoundedSemaphore(max_converting)
        self._lock = threading.Lock()
        self._held: dict[str, int] = {}
        self._engine = engine
        self._data = data_dir
        self._clock = clock
        self._convert = convert or convert_in_worker

    # -- what a content is -----------------------------------------------------------------
    @staticmethod
    def _key(params: RenditionParams, data: bytes) -> str:
        """The canonical parameters plus the hash of the bytes that are converted: the cache is
        keyed by what was really read, so another version of the file (the neutralized one, a
        newer derivative) or the page of another content never answers for this one."""
        body = json.loads(params.canonical())
        body["src"] = hashlib.sha256(data).hexdigest()
        return json.dumps(body, sort_keys=True, separators=(",", ":"))

    def _tombstoned(self, origin: _Origin) -> bool:
        """This content is being purged, or was: its own place is tombstoned. A file that
        carries several Units has no one place: the hash of its file then decides."""
        if origin.place is not None:
            return self._tombstones.has_values(origin.place)
        return self._tombstones.check_content([ContentHash(FILE_HASH_BY, origin.source)])

    def _refuse_purged(self, origin: _Origin) -> None:
        if self._tombstoned(origin):
            raise TombstonedContentError("the content was purged: nothing is made from it")

    def _origin(self, content_id: str) -> _Origin:
        c, u, e = storage.content, catalog.unit, catalog.edition
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(c.c.original_sha256, c.c.watermarked, c.c.unit_id).where(
                    c.c.id == content_id
                )
            ).first()
            if row is None:
                raise UnknownSourceError(content_id)
            place: PlaceRef | None = None
            if row.unit_id is not None:
                unit = connection.execute(
                    sa.select(
                        u.c.track,
                        u.c.number_segment,
                        u.c.number_raw,
                        u.c.part,
                        *(getattr(e.c, k) for k in EDITION_KEY_COLUMNS),
                    )
                    .select_from(u.join(e, e.c.id == u.c.edition_id))
                    .where(u.c.id == row.unit_id)
                ).first()
                if unit is not None:
                    key = {k: str(getattr(unit, k)) for k in EDITION_KEY_COLUMNS}
                    place = PlaceRef.of(
                        key, track=unit.track, number_segment=unit.number_segment,
                        number_raw=unit.number_raw, part=unit.part,
                    )  # fmt: skip
        return _Origin(str(row.original_sha256), bool(row.watermarked), place)

    # -- reading ---------------------------------------------------------------------------
    @classmethod
    def pinned(cls) -> frozenset[str]:
        """The renditions an in-flight request is reading right now (their content hashes): the
        retention sweep of the caches never evicts one of them (EF-BIB-018)."""
        with _PIN_LOCK:
            return frozenset(_PINNED)

    @contextmanager
    def serving(self, rendition: Rendition) -> Iterator[None]:
        """Pin `rendition` for the length of what the caller does with it: a read of the cache
        holds this for its own length, so a file a `/content` ticket is serving is never evicted
        under it. The registry is the process's, not the instance's: the sweep runs in the same
        process, on its own service."""
        key = rendition.content_hash
        with _PIN_LOCK:
            _PINNED[key] = _PINNED.get(key, 0) + 1
        try:
            yield
        finally:
            self._leave(key)

    def unpin(self, rendition: Rendition) -> None:
        """Drop the hold a lookup took: `cached`, `render` and `render_kepub` hand back a
        rendition **pinned** — the pin is taken before the check read, so the window from the
        lookup to the read is the request's, and a sweep that runs in it must find the pin.
        `holding` drops it at the end of its block; this is the same release, for a caller
        that cannot hold the block."""
        self._leave(rendition.content_hash)

    @contextmanager
    def holding(self, rendition: Rendition) -> Iterator[Rendition]:
        """The hold a lookup took, kept for the length of the block and dropped here whatever
        happens: the caller of `cached`, `render` or `render_kepub` wraps its read in this, and
        a sweep that runs inside the block leaves the file to the request. The pin itself was
        taken by the lookup, before its check read — never after it."""
        try:
            yield rendition
        finally:
            self._leave(rendition.content_hash)

    @classmethod
    def _leave(cls, key: str) -> None:
        with _PIN_LOCK:
            left = _PINNED[key] - 1
            if left:
                _PINNED[key] = left
            else:
                del _PINNED[key]

    @classmethod
    @contextmanager
    def erasable(cls, content_hash: str) -> Iterator[bool]:
        """The pin lock held around one erasure of a rendition file: True when no request holds
        `content_hash` (erase inside the block, the lock keeps a read from taking it meanwhile),
        False when one does — leave the file and its rows to the next pass. The sweep asks this
        at every file it unlinks: the picture it started from is already stale."""
        with _PIN_LOCK:
            yield content_hash not in _PINNED

    def _touch(self, folder: DataFolder, name: str) -> None:
        """Record the access on the file, unless it was recorded less than a day ago: the LRU
        key of the retention sweep is this time, never the creation."""
        stat = folder.stat_of(name)
        if stat is None:
            return
        at = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        if self._clock.now() - at < TOUCH_GRACE:
            return
        folder.touch(name, at=self._clock.now().timestamp())

    def read(self, rendition: Rendition) -> bytes | None:
        """The converted bytes, only when they are the ones whose hash is the name; None when
        the file is gone, is not a regular file, or was altered. Never a partial answer. The
        read pins the file for its own length (a ticket in flight is serving it) and records
        the access on it, at most once a day."""
        digest = _hex(rendition.content_hash)
        name = f"{digest}.{rendition.kind}"
        with self.serving(rendition):
            try:
                with open_data_folder(self._data, f"{CACHE_DIR}/{digest[:2]}") as folder:
                    data = folder.read(name, MAX_RENDITION_BYTES + 1)
                    if (
                        len(data) != rendition.size
                        or "sha256:" + hashlib.sha256(data).hexdigest() != rendition.content_hash
                    ):
                        return None
                    self._touch(folder, name)
                    return data
            except (OSError, ValueError):
                return None

    def cached(self, content_id: str, params: RenditionParams, data: bytes) -> Rendition | None:
        """The rendition already made from these bytes of this content, if its file is there and
        intact. Handed back **pinned**: the caller holds it with `holding` until it has read the
        file, so a sweep that runs between the lookup and the read keeps the file."""
        origin = self._origin(content_id)
        if self._tombstoned(origin):  # as `render` refuses: nothing is handed back during a purge
            return None
        return self._cached(origin.source, params, self._key(params, data))

    def _cached(self, source: str, params: RenditionParams, key: str) -> Rendition | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(storage.rendition.c.content_hash, storage.rendition.c.size).where(
                    storage.rendition.c.source_content_hash == source,
                    storage.rendition.c.kind == params.kind,
                    storage.rendition.c.params == _text(key),
                )
            ).first()
        if row is None:
            return None
        found = Rendition(source, params.kind, key, row.content_hash, row.size)
        self._pinned(found)  # before the check read: the hold covers it, and the window after
        if self.read(found) is None:
            self._leave(found.content_hash)  # the file is not there: the hold goes with it
            return None
        return found  # handed back pinned: the caller holds it with `holding` until it is read

    def _pinned(self, rendition: Rendition) -> Rendition:
        """Take the hold the caller will drop with `holding`: from the lookup (before its check
        read) to the read, the file is the request's, and a sweep that runs in that window finds
        the pin."""
        with _PIN_LOCK:
            _PINNED[rendition.content_hash] = _PINNED.get(rendition.content_hash, 0) + 1
        return rendition

    # -- the bound on threads --------------------------------------------------------------
    @contextmanager
    def slot(self, owner: str) -> Iterator[None]:
        """One of the threads the cache lends, for `owner` (a profile): refused at once, with no
        waiting, when all are held or when this profile holds its share. A caller takes it before
        it looks in the cache and keeps it until its answer is sent, so that a refusal never tells
        a cached book from one nobody made."""
        with self._lock:
            if (
                sum(self._held.values()) >= self._max_held
                or self._held.get(owner, 0) >= self._max_per_owner
            ):
                raise RenditionBusyError
            self._held[owner] = self._held.get(owner, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                self._held[owner] -= 1
                if not self._held[owner]:
                    del self._held[owner]

    # -- making ----------------------------------------------------------------------------
    def render(
        self,
        content_id: str,
        data: bytes,
        params: RenditionParams,
        *,
        processor: Processor = LOCAL_PROCESSOR,
        max_pixels: int | None = None,
        max_bytes: int | None = None,
    ) -> Rendition:
        """The rendition of `data`, read from the content `content_id`, made now if it is not
        there. The caps can be lowered by the caller, never raised past the worker's. Handed
        back **pinned**: the caller holds it with `holding` until it has read the file.

        Raises `UnknownSourceError`, `RemoteProcessorRefusedError` (a remote processor and a
        watermarked file), `UnsupportedSourceError` (not AVIF or WebP) and `ImageRejected`
        (`image.too_large`, `image.decode_failed`, `image.worker_failed`...)."""
        if params.kind == KEPUB_KIND:
            raise ValueError("a KEPUB is made by render_kepub, not by the image worker")
        origin = self._origin(content_id)
        source = origin.source
        self._refuse_purged(origin)
        ensure_processor_allowed(processor, watermarked=origin.watermarked)
        if processor != LOCAL_PROCESSOR:
            raise UnsupportedProcessorError(processor.name)  # none is shipped
        key = self._key(params, data)
        hit = self._cached(source, params, key)
        if hit is not None:
            return hit
        base = caps_for("page_input")
        if (max_pixels is not None and max_pixels <= 0) or (
            max_bytes is not None and max_bytes <= 0
        ):
            raise ImageRejected("image.too_large", "cap", "input")  # a cap of zero admits nothing
        caps = ImageCaps(
            min(base.max_pixels if max_pixels is None else max_pixels, base.max_pixels),
            min(base.max_bytes if max_bytes is None else max_bytes, base.max_bytes),
            formats=_FORMATS,
        )
        _check_source(data, max_bytes=caps.max_bytes, max_pixels=caps.max_pixels)
        made = self._convert(data, params, caps)  # the worker: its caps, memory, CPU, time; no net
        if made.format.lower() != params.kind or not made.data:
            raise ImageRejected("image.worker_failed", "bad reply", "worker")
        return self._store(origin, params, key, made.data)

    def render_kepub(self, content_id: str, data: bytes) -> Rendition:
        """The KEPUB of `data`, the neutralized EPUB read from the content `content_id`, made now
        if it is not there (rule X4: the caller judged the profile first and read these bytes from
        the content it names). Pure Python, nothing leaves the host, so nothing here depends on
        `watermarked`. Bounded by the size of the input, and by the conversions that run at once.
        Handed back **pinned**: the caller holds it with `holding` until it has read the file.

        Raises `UnknownSourceError`, `TombstonedContentError`, `RenditionBusyError` (no conversion
        slot came free in time) and `KepubError` (not an EPUB it can convert, or too large)."""
        origin = self._origin(content_id)
        self._refuse_purged(origin)
        if len(data) > self._max_kepub_bytes:
            raise KepubError("too_large", "the EPUB is larger than a KEPUB is made from")
        # the slot is taken before the cache is looked in and held for a hit too: with every slot
        # taken a book another profile made is refused like one nobody made (no oracle on it)
        if not self._converting.acquire(timeout=self._wait_s):
            raise RenditionBusyError
        try:
            key = self._key(KEPUB_PARAMS, data)
            hit = self._cached(origin.source, KEPUB_PARAMS, key)
            if hit is not None:
                return hit
            made = self._kepub(data)
            if not made or len(made) > MAX_RENDITION_BYTES:
                raise KepubError("bad_result", "the conversion gave nothing usable")
            return self._store(origin, KEPUB_PARAMS, key, made)
        finally:
            self._converting.release()

    def kepub_bytes(self, content_id: str, data: bytes) -> bytes:
        """`render_kepub`, then the bytes, checked against their hash as they are read. The
        lookup hands back a pinned rendition and `holding` keeps the hold until the read is
        done: a sweep that runs in the window leaves the file to the request."""
        rendition = self.render_kepub(content_id, data)
        with self.holding(rendition):
            found = self.read(rendition)
        if found is None:
            raise KepubError("unavailable", "the file of the KEPUB is gone")
        return found

    def _store(self, origin: _Origin, params: RenditionParams, key: str, data: bytes) -> Rendition:
        out_hash = "sha256:" + hashlib.sha256(data).hexdigest()
        rendition = Rendition(origin.source, params.kind, key, out_hash, len(data))
        write_data_file(self._data, rendition.rel, data)
        try:
            self._record(rendition, origin)
        except BaseException:
            self._drop_if_unreferenced(rendition.content_hash, rendition.kind)
            raise
        return self._pinned(rendition)  # handed back pinned, like a hit of the cache

    def _record(self, rendition: Rendition, origin: _Origin) -> None:
        table = storage.rendition
        stale: tuple[str, str] | None = None
        with write_transaction(self._engine) as connection:
            held = storage.content.c.original_sha256 == rendition.source_content_hash
            if not connection.execute(  # a purge may have taken the source since the read
                sa.select(sa.literal(1)).where(sa.exists().where(held))
            ).first():
                raise UnknownSourceError(rendition.source_content_hash)
            # read again under the write lock: a purge records its tombstones first, then lists
            # and erases the rows under this same lock, so a row written here is seen by it
            self._refuse_purged(origin)
            old = connection.execute(
                sa.select(table.c.content_hash, table.c.kind).where(
                    table.c.source_content_hash == rendition.source_content_hash,
                    table.c.kind == rendition.kind,
                    table.c.params == _text(rendition.params),
                )
            ).first()
            if old is not None and old.content_hash != rendition.content_hash:
                stale = (old.content_hash, old.kind)
            connection.execute(  # a stale row (its file altered or gone) is replaced
                sa.delete(table).where(
                    table.c.source_content_hash == rendition.source_content_hash,
                    table.c.kind == rendition.kind,
                    table.c.params == _text(rendition.params),
                )
            )
            connection.execute(
                sa.insert(table).values(
                    source_content_hash=rendition.source_content_hash,
                    kind=rendition.kind,
                    params=_text(rendition.params),
                    content_hash=rendition.content_hash,
                    size=rendition.size,
                    created_at=self._clock.now(),
                )
            )
        if stale is not None:
            self._drop_if_unreferenced(*stale)

    def _drop_if_unreferenced(self, content_hash: str, kind: str) -> None:
        """Erase a cache file no row points at (a render that failed after writing, the file a
        replaced row named). A file another row still names is that row's."""
        table = storage.rendition
        with self._engine.connect() as connection:
            used = connection.execute(
                sa.select(sa.func.count()).where(
                    table.c.content_hash == content_hash, table.c.kind == kind
                )
            ).scalar_one()
        if not used:
            digest = _hex(content_hash)
            _erase(self._data, f"{CACHE_DIR}/{digest[:2]}/{digest}.{kind}")


def collect_orphans(engine: sa.Engine, data_dir: Path, clock: Clock) -> int:
    """Erase the files of the cache that no row names and that are older than `STRAY_GRACE` (a
    render in flight is younger): what a crash between the file and the row, or a render that
    raced a purge, leaves behind. Names are checked; nothing else is touched. How many went. Run
    by the maintenance window job of the thumbnails (`jobs/thumbnail_gc.py`)."""
    table = storage.rendition
    with engine.connect() as connection:
        named = {
            (str(r.content_hash), str(r.kind))
            for r in connection.execute(sa.select(table.c.content_hash, table.c.kind))
        }
    cutoff = clock.now() - STRAY_GRACE
    gone = 0
    try:
        with open_data_folder(data_dir, CACHE_DIR) as root:
            shards = [n for n in root.names() if _SHARD.fullmatch(n)]
    except (OSError, ValueError):
        return 0
    for shard in shards:
        try:
            with open_data_folder(data_dir, f"{CACHE_DIR}/{shard}") as folder:
                for name in folder.names():
                    found = _NAME.fullmatch(name)
                    stat = folder.stat_of(name)
                    if found is None or stat is None or not stat_is_old(stat, cutoff):
                        continue
                    if (f"sha256:{found.group(1)}", found.group(2)) in named:
                        continue
                    if _erase(data_dir, f"{CACHE_DIR}/{shard}/{name}"):
                        gone += 1
        except (OSError, ValueError):
            continue
    return gone


def stat_is_old(stat: os.stat_result, cutoff: datetime) -> bool:
    return datetime.fromtimestamp(stat.st_mtime, tz=UTC) < cutoff


def _erase(data_dir: Path, rel: str) -> bool:
    try:
        return erase_data_path(data_dir, rel)
    except (OSError, ValueError):
        log.warning("rendition.not_erased")
        return False
