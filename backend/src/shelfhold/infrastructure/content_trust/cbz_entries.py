"""The CBZ entry allowlist (EF-BIB-051 (6)).

A comic archive from an untrusted source may hold only image pages (JPEG, PNG, WebP, GIF, AVIF,
JPEG XL) and one ComicInfo.xml. Each page's extension must agree with its magic bytes: an extension
is a promise, and a `.jpg` whose bytes are HTML or SVG is a scripted document in disguise. SVG,
HTML, XHTML, scripts and anything else off the list are refused with the stable code
`content.untrusted_entry`, unless the caller opts in with `allow_svg=True` (M2 L10): a `.svg` page
whose bytes open like an SVG is then listed in `svgs`, to be drawn to a PNG by the decode worker
(`packaging.cbz.rasterize_cbz`) before the archive is kept or served. HTML, XHTML and scripts stay
refused in every mode. ComicInfo.xml goes through the
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


# What an event may say about a refusal: a closed list (the detail below can carry the archive's
# own text, which an event never does).
REASON_KINDS: Final = frozenset(
    {"duplicate_comicinfo", "magic_mismatch", "no_pages", "not_allowlisted", "unsafe_archive",
     "unsafe_xml"}
)  # fmt: skip


class UntrustedEntryError(ValueError):
    """An entry is not on the allowlist. `code` is the stable error code, `reason` the detail."""

    code = UNTRUSTED_ENTRY_CODE

    def __init__(self, reason: str, entry: str | None = None) -> None:
        super().__init__(f"{UNTRUSTED_ENTRY_CODE}: {reason}" + (f" ({entry})" if entry else ""))
        self.reason = reason
        self.entry = entry

    @property
    def kind(self) -> str:
        """`reason` reduced to a member of `REASON_KINDS`."""
        if self.reason in REASON_KINDS:
            return self.reason
        return "unsafe_xml" if self.reason.startswith("xml_") else "unsafe_archive"


@dataclass(frozen=True, slots=True)
class CbzListing:
    """`images` are the raster pages, `svgs` the SVG pages (only with `allow_svg`), `pages` both in
    the archive's order."""

    images: tuple[str, ...]
    comicinfo: str | None
    svgs: tuple[str, ...] = ()
    pages: tuple[str, ...] = ()


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


# Page formats every reader takes: JPEG, PNG, GIF and WebP (EF-EXP-046, ADR-0054). Anything else is
# re-encoded, never trusted to a reader.
KEEP_PAGE_KINDS: Final = frozenset({"jpeg", "png", "gif", "webp"})
PAGE_EXTENSIONS: Final = {"jpeg": "jpg", "png": "png", "gif": "gif", "webp": "webp"}
_KIND_EXTENSIONS: Final = {
    "jpeg": frozenset({"jpg", "jpeg"}), "png": frozenset({"png"}), "gif": frozenset({"gif"}),
    "webp": frozenset({"webp"}),
}  # fmt: skip
_IMAGE_EXTENSIONS: Final = frozenset(_EXTENSION_KIND) | frozenset(
    {"bmp", "tif", "tiff", "heic", "heif", "jp2", "j2k", "jpx"}
)
_HEIF_BRANDS: Final = (b"heic", b"heix", b"hevc", b"hevx", b"heim", b"heis", b"mif1", b"msf1")


def sniff_page_kind(head: bytes) -> str | None:
    """The image kind the leading bytes announce, whether or not it is allowlisted: `jpeg`,
    `png`, `gif`, `webp`, `avif`, `jxl`, `bmp`, `tiff`, `heic` or `jp2`; None for anything else
    (and for an empty page). The extension plays no part."""
    kind = sniff_image_kind(head)
    if kind is not None:
        return kind
    if head.startswith(b"BM") and len(head) >= 14:
        return "bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "heic"
    if head.startswith((b"\x00\x00\x00\x0cjP  \r\n\x87\n", b"\xff\x4f\xff\x51")):
        return "jp2"
    return None


def page_extension(kind: str, current: str) -> str:
    """The extension a page of `kind` keeps (`current` if it already says so), else the usual."""
    known = _KIND_EXTENSIONS.get(kind)
    if known is None:  # a kind a policy keeps although it is off the allowlist: its name stays
        return current
    return current if current.lower() in known else PAGE_EXTENSIONS[kind]


def is_image_extension(extension: str) -> bool:
    return extension.lower() in _IMAGE_EXTENSIONS


def entry_extension(name: str) -> str:
    return _extension(name)


def sniff_svg(head: bytes) -> bool:
    """Do the leading bytes open like an SVG document (not HTML, not a binary)? The worker's audit
    is the real judge; this only keeps an extension honest."""
    text = head.removeprefix(b"\xef\xbb\xbf").lstrip(b" \t\r\n")
    return text.startswith((b"<svg", b"<?xml"))


def _extension(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def check_cbz_entries(
    archive: SafeArchive, *, allow_svg: bool = False, allow_convertible: bool = False
) -> CbzListing:
    """Refuse the archive on the first entry off the allowlist; otherwise list pages in order.

    `allow_convertible` (M4 L53) lets in a page of any raster format the decode worker can
    re-encode (BMP, TIFF, HEIC, JPEG 2000 on top of AVIF and JPEG XL), whatever its extension says:
    the magic bytes decide the kind. It is the door of an archive whose derivative the caller
    declares at once; an archive taken in this way is never served as it is."""
    images: list[str] = []
    svgs: list[str] = []
    pages: list[str] = []
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
        if allow_svg and _extension(name) == "svg":
            if not sniff_svg(archive.read_head(name, _HEAD_BYTES)):
                raise UntrustedEntryError("magic_mismatch", name)
            svgs.append(name)
            pages.append(name)
            continue
        if allow_convertible and is_image_extension(_extension(name)):
            if sniff_page_kind(archive.read_head(name, _HEAD_BYTES)) is None:
                raise UntrustedEntryError("magic_mismatch", name)
            images.append(name)
            pages.append(name)
            continue
        expected = _EXTENSION_KIND.get(_extension(name))
        if expected is None:
            raise UntrustedEntryError("not_allowlisted", name)
        if sniff_image_kind(archive.read_head(name, _HEAD_BYTES)) != expected:
            raise UntrustedEntryError("magic_mismatch", name)
        images.append(name)
        pages.append(name)
    if not pages:
        raise UntrustedEntryError("no_pages")
    return CbzListing(tuple(images), comicinfo, tuple(svgs), tuple(pages))


def _check_comicinfo(archive: SafeArchive, name: str) -> None:
    try:
        data = archive.read(name, max_bytes=COMICINFO_MAX_BYTES)
        safe_parse_xml(data, limits=XmlLimits(max_bytes=COMICINFO_MAX_BYTES))
    except UnsafeXmlError as error:
        raise UntrustedEntryError(f"xml_{error.reason}", name) from None
    except UnsafeArchiveError as error:
        raise UntrustedEntryError(error.reason, name) from None
