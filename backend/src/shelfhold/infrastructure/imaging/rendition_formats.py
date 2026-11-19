"""What a rendition converts from and to: the closed lists, shared by the planner and the service.

Pure data, no Pillow (EF-BIB-015, ADR-0054). A source is an image format a client may lack; a kind
is a format every client takes. JPEG XL is a source the worker has no decoder for (ADR-0054): it is
listed so that it is refused with its own code, and left out of what a plan may promise."""

from __future__ import annotations

from typing import Final

SOURCE_MEDIA: Final = {"avif": "image/avif", "jxl": "image/jxl", "webp": "image/webp"}
DECODABLE_SOURCES: Final = frozenset({"avif", "webp"})  # what the worker's Pillow reads
KIND_MEDIA: Final = {"jpeg": "image/jpeg", "png": "image/png"}
KEPUB_KIND: Final = "kepub"  # a book made from an EPUB (L20b), never planned for a page
ALL_KIND_MEDIA: Final = {**KIND_MEDIA, KEPUB_KIND: "application/kepub+zip"}
_ALIASES: Final = {"jpg": "jpeg", "image/jpg": "jpeg", **{m: k for k, m in KIND_MEDIA.items()}}


def target_kind(token: str) -> str | None:
    """The rendition kind a destination's `accepted_content_types` token names (a slug or a MIME
    type), or None when it names something a rendition does not make."""
    word = token.strip().lower()
    word = _ALIASES.get(word, word)
    return word if word in KIND_MEDIA else None


def source_format(format: str, media_type: str) -> str | None:
    """The source format of a `content` (its slug, or its MIME type), or None."""
    slug, media = format.strip().lower(), media_type.strip().lower()
    for name, mime in SOURCE_MEDIA.items():
        if slug == name or media == mime:
            return name
    return None
