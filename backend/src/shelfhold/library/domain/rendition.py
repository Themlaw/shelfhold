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
    ALL_KIND_MEDIA,
    DECODABLE_SOURCES,
    KEPUB_KIND,
    KIND_MEDIA,
    SOURCE_MEDIA,
    source_format,
    target_kind,
)

__all__ = [
    "ALL_KIND_MEDIA",
    "DECODABLE_SOURCES",
    "KEPUB_KIND",
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
    "webp_announced_size",
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
        if self.kind not in ALL_KIND_MEDIA:
            raise ValueError(f"kind must be one of {sorted(ALL_KIND_MEDIA)}")
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
_MAX_BOXES: Final = 1024
_MAX_DEPTH: Final = 6


def avif_announced_size(data: bytes) -> tuple[int, int] | None:
    """The largest `(width, height)` any `ispe` box of an AVIF declares (a grid declares one per
    tile and one for the whole image: the biggest is what a decoder may have to hold), or None
    when the walk does not find one: no `meta`, a malformed box, more boxes than the bound. The
    walk goes box to box by their sizes (a `free` box of any length is skipped, not read) through
    `meta > iprp > ipco`; nothing is decoded. A None is a refusal for the caller, never a pass."""
    seen = 0
    best: tuple[int, int] | None = None
    complete = True

    def walk(start: int, end: int, depth: int) -> None:
        nonlocal seen, best, complete
        at = start
        while at + 8 <= end:
            if seen >= _MAX_BOXES:
                complete = False
                return
            seen += 1
            size, kind = struct.unpack_from(">I4s", data, at)
            header = 8
            if size == 1:
                if at + 16 > end:
                    complete = False
                    return
                size, header = struct.unpack_from(">Q", data, at + 8)[0], 16
            elif size == 0:
                size = end - at
            if size < header:
                complete = False
                return
            body, stop = at + header, min(at + size, end)
            if kind == b"ispe":
                if body + 12 > stop:
                    complete = False
                    return
                found = struct.unpack_from(">II", data, body + 4)
                if best is None or found[0] * found[1] > best[0] * best[1]:
                    best = (found[0], found[1])
            elif kind in _CONTAINERS and depth < _MAX_DEPTH:
                walk(body + (4 if kind == b"meta" else 0), stop, depth + 1)
            at += size

    walk(0, len(data), 0)
    return best if complete else None


_MAX_CHUNKS: Final = 1024


def webp_announced_size(data: bytes) -> tuple[int, int] | None:
    """The largest `(width, height)` any chunk of a WebP announces (`VP8X`, `VP8`, `VP8L`), read
    by walking every RIFF chunk by its size (an odd one is padded to an even length). None when
    the file is not a coherent RIFF/WEBP (the RIFF size is longer than the bytes, a chunk runs past
    the end), when no chunk announces a size, or past the bound on chunks: a refusal for the
    caller, never a pass. A size can sit behind an alpha, animation or colour-profile chunk;
    nothing is decoded."""
    if len(data) < 12 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    end = 8 + struct.unpack_from("<I", data, 4)[0]
    if end > len(data) or end < 12:
        return None
    best: tuple[int, int] | None = None
    at = 12
    for _ in range(_MAX_CHUNKS):
        if at == end:
            return best
        if at + 8 > end:
            return None
        fourcc = data[at : at + 4]
        size = struct.unpack_from("<I", data, at + 4)[0]
        body = at + 8
        if body + size > end:
            return None
        found = _webp_chunk_size(fourcc, data[body : body + size])
        if found is not None and (best is None or found[0] * found[1] > best[0] * best[1]):
            best = found
        at = body + size + (size & 1)
    return None  # more chunks than the bound


def _webp_chunk_size(fourcc: bytes, payload: bytes) -> tuple[int, int] | None:
    le = int.from_bytes
    if fourcc == b"VP8X" and len(payload) >= 10:
        return 1 + le(payload[4:7], "little"), 1 + le(payload[7:10], "little")
    if fourcc == b"VP8 " and len(payload) >= 10 and payload[3:6] == b"\x9d\x01\x2a":
        return le(payload[6:8], "little") & 0x3FFF, le(payload[8:10], "little") & 0x3FFF
    if fourcc == b"VP8L" and len(payload) >= 5 and payload[0] == 0x2F:
        bits = le(payload[1:5], "little")
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    return None
