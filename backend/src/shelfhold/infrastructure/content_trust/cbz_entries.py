"""The CBZ entry allowlist (EF-BIB-051 (6)).

A comic archive from an untrusted source may hold only image pages (JPEG, PNG, WebP, GIF, AVIF,
JPEG XL) and one ComicInfo.xml. Each page's extension must agree with its magic bytes: an extension
is a promise, and a `.jpg` whose bytes are HTML or SVG is a scripted document in disguise. SVG,
HTML, XHTML, scripts and anything else off the list are refused with the stable code
`content.untrusted_entry` (M1a; M2 rasterises SVG pages instead). ComicInfo.xml goes through the
hardened XML entry point. Reading is bounded: 16 bytes per page, 1 MiB for ComicInfo.xml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from shelfhold.infrastructure.archives import SafeArchive, UnsafeArchiveError
from shelfhold.infrastructure.xml import UnsafeXmlError, XmlLimits, safe_parse_xml

UNTRUSTED_ENTRY_CODE: Final = "content.untrusted_entry"
COMICINFO_MAX_BYTES: Final = 1024 * 1024
_HEAD_BYTES: Final = 16

_EXTENSION_KIND: Final = {
    "jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp",
    "gif": "gif", "avif": "avif", "jxl": "jxl",
}  # fmt: skip


class UntrustedEntryError(ValueError):
    """An entry is not on the allowlist. `code` is the stable error code, `reason` the detail."""

    code = UNTRUSTED_ENTRY_CODE

    def __init__(self, reason: str, entry: str | None = None) -> None:
        super().__init__(f"{UNTRUSTED_ENTRY_CODE}: {reason}" + (f" ({entry})" if entry else ""))
        self.reason = reason
        self.entry = entry


@dataclass(frozen=True, slots=True)
class CbzListing:
    images: tuple[str, ...]
    comicinfo: str | None


def sniff_image_kind(head: bytes) -> str | None:
    """The allowlisted image kind the leading bytes announce, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head[4:8] == b"ftyp" and head[8:12] in (b"avif", b"avis"):
        return "avif"
    if head.startswith((b"\xff\x0a", b"\x00\x00\x00\x0cJXL \r\n\x87\n")):
        return "jxl"
    return None


def _extension(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def check_cbz_entries(archive: SafeArchive) -> CbzListing:
    """Refuse the archive on the first entry off the allowlist; otherwise list pages in order."""
    images: list[str] = []
    comicinfo: str | None = None
    for entry in archive.entries:
        if entry.is_dir:
            continue
        name = entry.name
        if name.rsplit("/", 1)[-1].lower() == "comicinfo.xml":
            if comicinfo is not None:
                raise UntrustedEntryError("duplicate_comicinfo", name)
            comicinfo = name
            _check_comicinfo(archive, name)
            continue
        expected = _EXTENSION_KIND.get(_extension(name))
        if expected is None:
            raise UntrustedEntryError("not_allowlisted", name)
        if sniff_image_kind(archive.read_head(name, _HEAD_BYTES)) != expected:
            raise UntrustedEntryError("magic_mismatch", name)
        images.append(name)
    if not images:
        raise UntrustedEntryError("no_pages")
    return CbzListing(tuple(images), comicinfo)


def _check_comicinfo(archive: SafeArchive, name: str) -> None:
    try:
        data = archive.read(name, max_bytes=COMICINFO_MAX_BYTES)
        safe_parse_xml(data, limits=XmlLimits(max_bytes=COMICINFO_MAX_BYTES))
    except UnsafeXmlError as error:
        raise UntrustedEntryError(f"xml_{error.reason}", name) from None
    except UnsafeArchiveError as error:
        raise UntrustedEntryError(error.reason, name) from None
