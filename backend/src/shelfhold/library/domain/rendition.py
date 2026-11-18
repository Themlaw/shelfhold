"""A rendition: an image converted for a client that lacks its format (EF-BIB-015).

Pure rules, nothing is read or written here:
- `RenditionParams`: the closed set of what a conversion is asked (kind, quality, the entry of the
  archive it is about), with one canonical form, the key of the cache;
- `Processor` and `ensure_processor_allowed`: a processor that sends bytes out of the host
  (`egress = externe`) is never given a `watermarked` file (EF-EXP-058, ENF-LEG-009); the local
  one is, since nothing leaves. No remote processor is shipped; the rule is in place for the first;
- `avif_announced_size`: the size an AVIF states in its header, read before any decoder starts.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Final

from shelfhold.infrastructure.imaging.rendition_formats import (
    DECODABLE_SOURCES,
    KIND_MEDIA,
    SOURCE_MEDIA,
    source_format,
    target_kind,
)

__all__ = [
    "DECODABLE_SOURCES",
    "KIND_MEDIA",
    "LOCAL_PROCESSOR",
    "SOURCE_MEDIA",
    "Processor",
    "RemoteProcessorRefusedError",
    "RenditionParams",
    "UnsupportedProcessorError",
    "UnsupportedSourceError",
    "avif_announced_size",
    "ensure_processor_allowed",
    "sniff_source",
    "source_format",
    "target_kind",
]

REMOTE_REFUSED_WATERMARKED: Final = "rendition.remote_refused_watermarked"
FORMAT_UNSUPPORTED: Final = "rendition.format_unsupported"
LOCAL_EGRESS: Final = "foyer"  # the word a destination uses for "stays in the home"


class RemoteProcessorRefusedError(RuntimeError):
    """A processor that sends bytes out of the host asked for a watermarked file."""

    code = REMOTE_REFUSED_WATERMARKED


class UnsupportedSourceError(ValueError):
    """The bytes are not a format a rendition converts from (or one the worker cannot decode)."""

    code = FORMAT_UNSUPPORTED


class UnsupportedProcessorError(ValueError):
    """A processor other than the local worker: none is shipped, so none runs (a programming
    error, never something a request can cause)."""


@dataclass(frozen=True, slots=True)
class Processor:
    name: str
    egress: str = LOCAL_EGRESS


LOCAL_PROCESSOR: Final = Processor("local-worker")


def ensure_processor_allowed(processor: Processor, *, watermarked: bool) -> None:
    """Refuse a processor that is not the local one on a watermarked file. Any egress word other
    than `foyer` counts as leaving: the rule fails closed."""
    if watermarked and processor.egress != LOCAL_EGRESS:
        raise RemoteProcessorRefusedError(processor.name)


@dataclass(frozen=True, slots=True)
class RenditionParams:
    kind: str
    quality: int = 85
    entry: str | None = None  # the page of an archive (None: the content is one image)

    def __post_init__(self) -> None:
        if self.kind not in KIND_MEDIA:
            raise ValueError(f"kind must be one of {sorted(KIND_MEDIA)}")
        if not 1 <= self.quality <= 100:
            raise ValueError("quality must be between 1 and 100")
        if self.entry is not None and not 0 < len(self.entry) <= 512:
            raise ValueError("entry must be a name of at most 512 characters")

    def canonical(self) -> str:
        """The JSON the ledger keeps: sorted keys, no space. A PNG has no quality, so it is left
        out: two asks for the same PNG are one rendition."""
        body: dict[str, object] = {"entry": self.entry} if self.entry is not None else {}
        if self.kind == "jpeg":
            body["quality"] = self.quality
        return json.dumps(body, sort_keys=True, separators=(",", ":"))


def sniff_source(head: bytes) -> str | None:
    """`avif`, `webp` or `jxl` by the magic bytes of the file, or None. Only what the first bytes
    say; the worker is the one that decides whether it is really that."""
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis", b"mif1"):
        return "avif"
    if head[:2] == b"\xff\x0a" or head[:12] == b"\x00\x00\x00\x0cJXL \r\n\x87\n":
        return "jxl"
    return None


_CONTAINERS: Final = (b"meta", b"iprp", b"ipco")
_MAX_BOXES: Final = 256
_MAX_DEPTH: Final = 6


def avif_announced_size(head: bytes) -> tuple[int, int] | None:
    """`(width, height)` from the `ispe` box of an AVIF, or None when the bytes given do not reach
    it. A bounded walk (depth and box count) over `meta > iprp > ipco`; nothing is decoded."""
    seen = 0

    def walk(start: int, end: int, depth: int) -> tuple[int, int] | None:
        nonlocal seen
        at = start
        while at + 8 <= end and seen < _MAX_BOXES:
            seen += 1
            size, kind = struct.unpack_from(">I4s", head, at)
            header = 8
            if size == 1:
                if at + 16 > end:
                    return None
                size, header = struct.unpack_from(">Q", head, at + 8)[0], 16
            elif size == 0:
                size = end - at
            if size < header:
                return None
            body, stop = at + header, min(at + size, end)
            if kind == b"ispe":
                if body + 12 > stop:
                    return None
                return struct.unpack_from(">II", head, body + 4)
            if kind in _CONTAINERS and depth < _MAX_DEPTH:
                inner = walk(body + (4 if kind == b"meta" else 0), stop, depth + 1)
                if inner is not None:
                    return inner
            at += size
        return None

    return walk(0, len(head), 0)
