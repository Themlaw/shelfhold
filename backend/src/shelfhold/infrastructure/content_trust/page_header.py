"""The size a page announces in its header, read without decoding a pixel (M4 L53, ADR-0054).

A JPEG, PNG, GIF or WebP page is kept as it is, so no decoder looks at it before it reaches a
reader, and MuPDF on an e-reader or the cover of Kavita decodes it at full size. Its width and
height are in the first bytes (IHDR, SOFn, VP8X/VP8/VP8L, the logical screen of a GIF): reading
them costs nothing and refuses a bomb before it is filed or sent.
"""

from __future__ import annotations

from typing import Final

__all__ = ["HEADER_STEPS", "announced_size"]

HEADER_STEPS: Final = (4096, 65536, 1 << 20)  # how much is read, in turn, to find a JPEG's SOF
_SOF: Final = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


def _be16(data: bytes, at: int) -> int:
    return (data[at] << 8) | data[at + 1]


def _le(data: bytes, at: int, size: int) -> int:
    return int.from_bytes(data[at : at + size], "little")


def _png(head: bytes) -> tuple[int, int] | None:
    if len(head) < 24 or head[12:16] != b"IHDR":
        return None
    return int.from_bytes(head[16:20], "big"), int.from_bytes(head[20:24], "big")


def _gif(head: bytes) -> tuple[int, int] | None:
    return (_le(head, 6, 2), _le(head, 8, 2)) if len(head) >= 10 else None


def _webp(head: bytes) -> tuple[int, int] | None:
    kind = head[12:16]
    if kind == b"VP8X" and len(head) >= 30:
        return 1 + _le(head, 24, 3), 1 + _le(head, 27, 3)
    if kind == b"VP8 " and len(head) >= 30 and head[23:26] == b"\x9d\x01\x2a":
        return _le(head, 26, 2) & 0x3FFF, _le(head, 28, 2) & 0x3FFF
    if kind == b"VP8L" and len(head) >= 25 and head[20] == 0x2F:
        bits = _le(head, 21, 4)
        return 1 + (bits & 0x3FFF), 1 + ((bits >> 14) & 0x3FFF)
    return None


def _jpeg(head: bytes) -> tuple[int, int] | bool | None:
    """(width, height); None when the data ends before a frame header (read more); False when no
    frame header can come (a scan or the end of the image first)."""
    i = 2
    while i + 4 <= len(head):
        if head[i] != 0xFF:
            i += 1
            continue
        marker = head[i + 1]
        if marker == 0xFF:  # fill byte
            i += 1
            continue
        if marker in _SOF:
            if i + 9 > len(head):
                return None
            return _be16(head, i + 7), _be16(head, i + 5)
        if marker in (0xDA, 0xD9):
            return False
        if marker == 0x01 or 0xD0 <= marker <= 0xD8:  # standalone markers
            i += 2
            continue
        i += 2 + _be16(head, i + 2)
    return None


def announced_size(head: bytes, kind: str) -> tuple[int, int] | None:
    """`(width, height)` the header of a `kind` page announces, None when it does not say (or the
    bytes given end too soon: `needs_more` tells a JPEG to read further)."""
    if kind == "png":
        return _png(head)
    if kind == "gif":
        return _gif(head)
    if kind == "webp":
        return _webp(head)
    if kind == "jpeg":
        found = _jpeg(head)
        return found if isinstance(found, tuple) else None
    return None


def needs_more(head: bytes, kind: str) -> bool:
    """A JPEG whose frame header is not in these bytes yet."""
    return kind == "jpeg" and _jpeg(head) is None
