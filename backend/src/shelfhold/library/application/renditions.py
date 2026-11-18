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
rows (`rendition_rels`, `erase_renditions` of the purge inventory, ADR-0048). The source is not
modified, a `watermarked` file included: the converted file is a separate one that stays at home.
Nothing here knows who may see a content: that is the caller's decision, taken first.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import sqlalchemy as sa
import structlog

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.content_trust.page_header import announced_size
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.imaging import ImageRejected, Reencoded, caps_for, reencode_image
from shelfhold.infrastructure.schema import storage
from shelfhold.infrastructure.volumes import erase_data_path, open_data_folder, write_data_file
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

__all__ = [
    "CACHE_DIR",
    "Converter",
    "Rendition",
    "RenditionService",
    "UnknownSourceError",
]

log = structlog.get_logger(__name__)

CACHE_DIR: Final = "cache/renditions"  # under /data
MAX_RENDITION_BYTES: Final = 128 * 1024 * 1024  # the byte cap of a page: what may be read back
Converter = Callable[[bytes, RenditionParams], Reencoded]


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


def convert_in_worker(data: bytes, params: RenditionParams) -> Reencoded:
    """The default converter: the bounded, network-less decode worker (`page_input` caps)."""
    return reencode_image(data, fmt=params.kind, quality=params.quality)


def _check_source(data: bytes, *, max_bytes: int, max_pixels: int) -> None:
    """Everything that can be said about the bytes without decoding a pixel."""
    if len(data) > max_bytes:
        raise ImageRejected("image.too_large", "bytes", "input")
    found = sniff_source(data[:64])
    if found not in DECODABLE_SOURCES:  # JPEG XL has no decoder; anything else is not a source
        raise UnsupportedSourceError(found or "unknown")
    head = data[: 1 << 16]
    announced = avif_announced_size(head) if found == "avif" else announced_size(head, found)
    if announced is not None and announced[0] * announced[1] > max_pixels:
        raise ImageRejected("image.too_large", "pixels", "header")


class RenditionService:
    def __init__(
        self,
        engine: sa.Engine,
        data_dir: Path,
        clock: Clock,
        *,
        convert: Converter | None = None,
    ) -> None:
        self._engine = engine
        self._data = data_dir
        self._clock = clock
        self._convert = convert or convert_in_worker

    # -- what a content is -----------------------------------------------------------------
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

    def cached(self, content_id: str, params: RenditionParams) -> Rendition | None:
        """The rendition already made for this content and these parameters, if its file is
        there and intact."""
        source, _ = self._source(content_id)
        return self._cached(source, params)

    def _cached(self, source: str, params: RenditionParams) -> Rendition | None:
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(storage.rendition.c.content_hash, storage.rendition.c.size).where(
                    storage.rendition.c.source_content_hash == source,
                    storage.rendition.c.kind == params.kind,
                    storage.rendition.c.params == _text(params.canonical()),
                )
            ).first()
        if row is None:
            return None
        found = Rendition(source, params.kind, params.canonical(), row.content_hash, row.size)
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
        ensure_processor_allowed(processor, watermarked=watermarked)
        if processor != LOCAL_PROCESSOR:
            raise UnsupportedProcessorError(processor.name)  # none is shipped
        hit = self._cached(source, params)
        if hit is not None:
            return hit
        caps = caps_for("page_input")
        _check_source(
            data,
            max_bytes=min(max_bytes or caps.max_bytes, caps.max_bytes),
            max_pixels=min(max_pixels or caps.max_pixels, caps.max_pixels),
        )
        made = self._convert(data, params)  # the worker: own caps, memory, CPU, time; no network
        if made.format.lower() != params.kind or not made.data:
            raise ImageRejected("image.worker_failed", "bad reply", "worker")
        out_hash = "sha256:" + hashlib.sha256(made.data).hexdigest()
        rendition = Rendition(source, params.kind, params.canonical(), out_hash, len(made.data))
        write_data_file(self._data, rendition.rel, made.data)
        self._record(rendition)
        return rendition

    def _record(self, rendition: Rendition) -> None:
        table = storage.rendition
        with write_transaction(self._engine) as connection:
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


def erase_rendition_files(data_dir: Path, rels: list[str]) -> int:
    """Erase these cache files (the purge's step, before the rows); how many went."""
    return sum(1 for rel in rels if _erase(data_dir, rel))


def _erase(data_dir: Path, rel: str) -> bool:
    try:
        return erase_data_path(data_dir, rel)
    except (OSError, ValueError):
        log.warning("rendition.not_erased")
        return False
