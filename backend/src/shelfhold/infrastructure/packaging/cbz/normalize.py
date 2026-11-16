"""Bring the pages of a CBZ to formats every reader takes (EF-EXP-046 amended by ADR-0054).

JPEG, PNG, GIF and WebP stay, recognised by their magic bytes (`sniff_page_kind`) and never by the
extension. Any other image (AVIF, JPEG XL, HEIC, BMP, TIFF, JPEG 2000...) is re-encoded by the
decode worker to the target format (JPEG at quality 90 by default, alpha flattened on white).

The rewrite keeps what the reader keys on: the pages stay in the archive's order and under their
names, only the extension changes; two pages that would end with the same name are told apart with
`-1`, `-2`... before the extension, the first keeping its name. Anything that is not an image is
copied as it is, `ComicInfo.xml` last (Kavita reads it from the end) with the size and dimensions
of each re-encoded page in its `<Page>` elements brought up to date and nothing else touched.

All or nothing: an empty page, a page that cannot be decoded or is too large, or a format no
decoder reads gives a closed code (`page.*`), the archive is left as it was received (`kept`) and
nothing is written to `out`. An archive with nothing to change is not rewritten (`unchanged`: its
hash does not move), so running it again changes nothing. The caller writes to a staging file and
renames it with fsops.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import BinaryIO, Final, Literal

from shelfhold.infrastructure.archives import ArchiveWriter, SafeArchive, UnsafeArchiveError
from shelfhold.infrastructure.archives.safe_open import _dedup_key
from shelfhold.infrastructure.content_trust.cbz_entries import (
    KEEP_PAGE_KINDS,
    entry_extension,
    is_image_extension,
    page_extension,
    sniff_page_kind,
)
from shelfhold.infrastructure.imaging import ImageRejected, Reencoded, reencode_image
from shelfhold.infrastructure.imaging.errors import (
    FORMAT_REFUSED,
    PAGE_EMPTY,
    PAGE_FORMAT_UNSUPPORTED,
    PAGE_TOO_LARGE,
    PAGE_UNDECODABLE,
    TOO_LARGE,
    WORKER_FAILED,
)

COMICINFO_NAME: Final = "ComicInfo.xml"
PAGE_CODES: Final = (PAGE_EMPTY, PAGE_UNDECODABLE, PAGE_TOO_LARGE, PAGE_FORMAT_UNSUPPORTED)
TARGET_FORMATS: Final = ("jpeg", "png", "webp")
_TARGET_EXTENSION: Final = {"jpeg": "jpg", "png": "png", "webp": "webp"}
_NO_DECODER: Final = frozenset({"jxl", "heic"})  # Pillow ships no reader for them
_HEAD_BYTES: Final = 32
_PAGE_TAG: Final = re.compile(rb"<Page\b[^>]*>")
_IMAGE_INDEX: Final = re.compile(rb'\bImage="(\d+)"')


@dataclass(frozen=True, slots=True)
class PageTarget:
    """What a page that is not kept becomes: `format` (`jpeg`, `png` or `webp`; never AVIF or JXL)
    and the `quality` of JPEG and WebP (PNG is lossless)."""

    format: str = "jpeg"
    quality: int = 90

    def __post_init__(self) -> None:
        if self.format not in TARGET_FORMATS:
            raise ValueError(f"page target format must be one of {TARGET_FORMATS}")
        if not 1 <= self.quality <= 100:
            raise ValueError("page target quality must be between 1 and 100")

    @property
    def extension(self) -> str:
        return _TARGET_EXTENSION[self.format]


@dataclass(frozen=True, slots=True)
class PageIssue:
    entry: str
    code: str  # one of PAGE_CODES


@dataclass(frozen=True, slots=True)
class NormalizeReport:
    """`unchanged`: nothing to do, nothing written. `normalized`: `out` holds the new archive.
    `kept`: a page raised an issue, nothing was written, the archive stays as received."""

    status: Literal["unchanged", "normalized", "kept"]
    converted: int = 0
    renamed: int = 0
    issues: tuple[PageIssue, ...] = ()
    names: tuple[tuple[str, str], ...] = field(default=())  # (old, new), pages in archive order


Converter = Callable[[bytes, PageTarget], Reencoded]


def convert_with_worker(data: bytes, target: PageTarget) -> Reencoded:
    return reencode_image(data, fmt=target.format, quality=target.quality)


@dataclass(slots=True)
class _Item:
    name: str
    role: Literal["page", "other", "comicinfo"]
    kind: str | None = None
    ext: str = ""
    index: int = -1  # position among the pages, what ComicInfo's `Image` counts
    convert: bool = False
    new_name: str = ""


def _classify(source: SafeArchive, keep: frozenset[str]) -> tuple[list[_Item], list[PageIssue]]:
    items: list[_Item] = []
    issues: list[PageIssue] = []
    seen_comicinfo = False
    index = 0
    for entry in source.entries:
        if entry.is_dir:
            continue
        name = entry.name
        if name.rsplit("/", 1)[-1].lower() == COMICINFO_NAME.lower() and not seen_comicinfo:
            seen_comicinfo = True
            items.append(_Item(name, "comicinfo"))
            continue
        ext = entry_extension(name)
        head = source.read_head(name, _HEAD_BYTES)
        kind = sniff_page_kind(head)
        if kind is None:
            if not is_image_extension(ext):
                items.append(_Item(name, "other"))  # a text, a sidecar: copied as it is
                continue
            issues.append(PageIssue(name, PAGE_EMPTY if not head else PAGE_UNDECODABLE))
            items.append(_Item(name, "page", None, ext, index))
        else:
            item = _Item(name, "page", kind, ext, index, convert=kind not in keep)
            items.append(item)
        index += 1
    return items, issues


def _stem(name: str) -> str:
    base = name.rsplit("/", 1)[-1]
    return name[: len(name) - len(base)] + (base.rsplit(".", 1)[0] if "." in base else base)


def _assign_names(items: list[_Item], target: PageTarget) -> int:
    """New names, in archive order. A name that does not change is reserved first, so the page
    that already has it keeps it; a page that moves to a taken name gets `-1`, `-2`..."""
    taken: set[str] = set()
    for item in items:
        if (
            item.role != "page"
            or item.kind is None
            or (not item.convert and page_extension(item.kind, item.ext) == item.ext)
        ):
            item.new_name = item.name
            taken.add(_dedup_key(item.name))
    renamed = 0
    for item in items:
        if item.new_name:
            continue
        extension = target.extension if item.convert else page_extension(str(item.kind), item.ext)
        stem = _stem(item.name)
        candidate, n = f"{stem}.{extension}", 0
        while _dedup_key(candidate) in taken:
            n += 1
            candidate = f"{stem}-{n}.{extension}"
        item.new_name = candidate
        taken.add(_dedup_key(candidate))
        renamed += 1
    return renamed


def update_page_facts(xml: bytes, facts: dict[int, tuple[int, int, int]]) -> bytes:
    """`ImageSize`, `ImageWidth` and `ImageHeight` of the `<Page Image="n">` elements named in
    `facts` (`n -> (bytes, width, height)`), only where the attribute is already there. Every
    other byte of the document is the one it was."""

    def patch(match: re.Match[bytes]) -> bytes:
        tag = match.group(0)
        found = _IMAGE_INDEX.search(tag)
        values = facts.get(int(found.group(1))) if found else None
        if values is None:
            return tag
        for attribute, value in zip(
            (b"ImageSize", b"ImageWidth", b"ImageHeight"), values, strict=True
        ):
            tag = re.sub(
                rb"(\b" + attribute + rb'=")[^"]*(")', rb"\g<1>" + b"%d" % value + rb"\g<2>", tag
            )
        return tag

    return _PAGE_TAG.sub(patch, xml)


def needs_normalization(source: SafeArchive, keep: frozenset[str] = KEEP_PAGE_KINDS) -> bool:
    """Would `normalize_cbz` change this archive, or report a page? Names and the first bytes of
    each page only: no page is decoded, so it is cheap on an archive that is already clean."""
    items, issues = _classify(source, keep)
    return bool(issues) or any(i.convert for i in items) or _assign_names(items, PageTarget()) > 0


def _issue_for(error: ImageRejected, item: _Item) -> PageIssue:
    if error.code == TOO_LARGE:
        return PageIssue(item.name, PAGE_TOO_LARGE)
    if error.code == FORMAT_REFUSED and item.kind in _NO_DECODER:
        return PageIssue(item.name, PAGE_FORMAT_UNSUPPORTED)
    return PageIssue(item.name, PAGE_UNDECODABLE)


def normalize_cbz(
    source: SafeArchive,
    out: BinaryIO,
    *,
    target: PageTarget | None = None,
    keep: frozenset[str] = KEEP_PAGE_KINDS,
    convert: Converter = convert_with_worker,
    comicinfo: bytes | None = None,
) -> NormalizeReport:
    """Rewrite `source` into `out` as described above; see the module docstring.

    `keep` is the set of kinds the destination takes as they are (a subset of the allowlist);
    every other kind is converted. `comicinfo`, when given, replaces the archive's own (the
    exporter's): it is written last as it is and the page facts are not patched. A worker that
    fails by itself (`image.worker_failed`) is not the page's fault: it propagates, and the caller
    asks again later."""
    target = target or PageTarget()
    items, issues = _classify(source, keep)
    renamed = _assign_names(items, target)
    pages = [i for i in items if i.role == "page"]
    names = tuple((i.name, i.new_name) for i in pages)
    to_convert = [i for i in pages if i.convert]
    if not issues and not to_convert and renamed == 0:
        return NormalizeReport("unchanged", names=names)

    own_comicinfo = next((i for i in items if i.role == "comicinfo"), None)
    facts: dict[int, tuple[int, int, int]] = {}
    found: list[PageIssue] = list(issues)  # what the names showed; decoding adds to it
    writer = ArchiveWriter(out)
    try:
        for item in items:
            if item.role == "comicinfo":
                continue
            if item.role == "other" or not item.convert:
                if not found:
                    with source.open(item.name) as stream:
                        writer.add(item.new_name, stream, compress=item.role == "other")
                continue
            if item.kind is None:
                continue  # empty or not an image: already in `found`
            try:
                data = bytes(source.read(item.name, max_bytes=_page_limit()))
                result = convert(data, target)
            except UnsafeArchiveError:
                found.append(PageIssue(item.name, PAGE_TOO_LARGE))
                continue
            except ImageRejected as error:
                if error.code == WORKER_FAILED:
                    raise
                found.append(_issue_for(error, item))
                continue
            if found:
                continue  # already refused: the rest is only looked at, not written
            facts[item.index] = (len(result.data), result.width, result.height)
            writer.add(item.new_name, result.data)
        if not found and comicinfo is not None:
            writer.add(COMICINFO_NAME, comicinfo, compress=True)
        elif not found and own_comicinfo is not None:
            raw = bytes(source.read(own_comicinfo.name, max_bytes=1024 * 1024))
            writer.add(COMICINFO_NAME, update_page_facts(raw, facts), compress=True)
    finally:
        writer.close()
    if found:
        out.seek(0)
        out.truncate(0)
        return NormalizeReport("kept", issues=tuple(found))
    return NormalizeReport("normalized", len(to_convert), renamed - len(to_convert), (), names)


def _page_limit() -> int:
    from shelfhold.infrastructure.imaging import caps_for

    return caps_for("page_input").max_bytes
