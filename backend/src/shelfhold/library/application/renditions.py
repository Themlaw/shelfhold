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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final, Protocol

import sqlalchemy as sa
import structlog

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust.page_header import announced_size
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.imaging import (
    ImageCaps,
    ImageRejected,
    Reencoded,
    caps_for,
    reencode_image,
)
from shelfhold.infrastructure.schema import storage
from shelfhold.infrastructure.volumes import erase_data_path, open_data_folder, write_data_file
from shelfhold.library.domain.content_hash import FILE_HASH_BY
from shelfhold.library.domain.failures import TombstonedContentError
from shelfhold.library.domain.rendition import (
    DECODABLE_SOURCES,
    KIND_MEDIA,
    LOCAL_PROCESSOR,
    Processor,
    RenditionParams,
    UnsupportedProcessorError,
    UnsupportedSourceError,
    avif_announced_size,
    ensure_processor_allowed,
    sniff_source,
)
from shelfhold.library.tombstones.values import ContentHash

__all__ = [
    "CACHE_DIR",
    "STRAY_GRACE",
    "Converter",
    "Rendition",
    "RenditionService",
    "UnknownSourceError",
    "collect_orphans",
]

log = structlog.get_logger(__name__)

CACHE_DIR: Final = "cache/renditions"  # under /data
MAX_RENDITION_BYTES: Final = 128 * 1024 * 1024  # the byte cap of a page: what may be read back
Converter = Callable[[bytes, RenditionParams, ImageCaps], Reencoded]
STRAY_GRACE: Final = timedelta(minutes=10)  # a render writes its file, then its row: seconds
_SHARD: Final = re.compile(r"[0-9a-f]{2}")
_NAME: Final = re.compile(r"([0-9a-f]{64})\.(jpeg|png)")
_FORMATS: Final = ("WEBP", "AVIF")  # what the worker is allowed to open for a rendition


class SourceTombstones(Protocol):
    """The part of the tombstone service a render needs: is this file purged, or being purged?
    A purge records its tombstones before it deletes anything (ADR-0048)."""

    def check_content(self, hashes: Iterable[ContentHash] = ()) -> bool: ...


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
        return KIND_MEDIA[self.kind]

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
    head = data[: 1 << 16]
    if found == "avif":
        announced = avif_announced_size(data)  # the whole file is in hand: boxes are walked by size
        if announced is None:  # no `meta`, no `ispe`, a malformed box: not a header to trust
            raise ImageRejected("image.format_refused", "avif header", "header")
    else:
        announced = announced_size(head, found)
    if announced is not None and announced[0] * announced[1] > max_pixels:
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
    ) -> None:
        self._tombstones = tombstones
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

    def _refuse_purged(self, source: str) -> None:
        if self._tombstones.check_content([ContentHash(FILE_HASH_BY, source)]):
            raise TombstonedContentError("the content was purged: nothing is made from it")

    def _source(self, content_id: str) -> tuple[str, bool]:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(storage.content.c.original_sha256, storage.content.c.watermarked).where(
                    storage.content.c.id == content_id
                )
            ).first()
        if row is None:
            raise UnknownSourceError(content_id)
        return str(row.original_sha256), bool(row.watermarked)

    # -- reading ---------------------------------------------------------------------------
    def read(self, rendition: Rendition) -> bytes | None:
        """The converted bytes, only when they are the ones whose hash is the name; None when
        the file is gone, is not a regular file, or was altered. Never a partial answer."""
        digest = _hex(rendition.content_hash)
        try:
            with open_data_folder(self._data, f"{CACHE_DIR}/{digest[:2]}") as folder:
                data = folder.read(f"{digest}.{rendition.kind}", MAX_RENDITION_BYTES + 1)
        except (OSError, ValueError):
            return None
        if len(data) != rendition.size or "sha256:" + hashlib.sha256(data).hexdigest() != (
            rendition.content_hash
        ):
            return None
        return data

    def cached(self, content_id: str, params: RenditionParams, data: bytes) -> Rendition | None:
        """The rendition already made from these bytes of this content, if its file is there and
        intact."""
        source, _ = self._source(content_id)
        return self._cached(source, params, self._key(params, data))

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
        return found if self.read(found) is not None else None

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
        there. The caps can be lowered by the caller, never raised past the worker's.

        Raises `UnknownSourceError`, `RemoteProcessorRefusedError` (a remote processor and a
        watermarked file), `UnsupportedSourceError` (not AVIF or WebP) and `ImageRejected`
        (`image.too_large`, `image.decode_failed`, `image.worker_failed`...)."""
        source, watermarked = self._source(content_id)
        self._refuse_purged(source)
        ensure_processor_allowed(processor, watermarked=watermarked)
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
        out_hash = "sha256:" + hashlib.sha256(made.data).hexdigest()
        rendition = Rendition(source, params.kind, key, out_hash, len(made.data))
        write_data_file(self._data, rendition.rel, made.data)
        try:
            self._record(rendition)
        except BaseException:
            self._drop_if_unreferenced(rendition.content_hash, rendition.kind)
            raise
        return rendition

    def _record(self, rendition: Rendition) -> None:
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
            self._refuse_purged(rendition.source_content_hash)
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

    def collect_orphans(self) -> int:
        """See `collect_orphans`: the same sweep, with this service's engine, folder and clock."""
        return collect_orphans(self._engine, self._data, self._clock)


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
