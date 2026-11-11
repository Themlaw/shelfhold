"""The table of contents of an EPUB, read for its chapter bounds (EF-LEC-014).

The chapters of a volume that a source delivers as one EPUB are the entries of its table of
contents: the EPUB 3 `nav` document, or the NCX of an EPUB 2. Each leaf entry points at one item
of the reading order (the spine), which gives the chapter its place in the file: that place is the
bound Shelfhold keeps in `unit_coverage` when the source gave none.

The EPUB is untrusted: the archive is opened by `safe_open_archive`, every XML document by
`safe_parse_xml`, and a `href` that leaves the archive or names no manifest item is not a chapter
(it is listed in `Toc.unresolved`, never followed).
"""

from __future__ import annotations

import hashlib
import posixpath
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Final

from shelfhold.infrastructure.archives import (
    DEFAULT_LIMITS,
    ArchiveLimits,
    SafeArchive,
    UnsafeArchiveError,
    safe_open_archive,
)
from shelfhold.infrastructure.packaging.epub.opf import find_opf_path
from shelfhold.infrastructure.xml import Element, UnsafeXmlError, safe_parse_xml

__all__ = ["Toc", "TocChapter", "TocError", "read_toc"]

_MAX_DOC: Final = 4_000_000
_NCX_TYPE: Final = "application/x-dtbncx+xml"


class TocError(ValueError):
    """The table of contents cannot be read; `reason` is a stable word."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class TocChapter:
    title: str
    path: str  # the archive path of the item, normalised
    fragment: str  # the part after `#`, "" when none
    spine_index: int  # the place of the item in the reading order

    @property
    def target(self) -> str:
        return f"{self.path}#{self.fragment}" if self.fragment else self.path


@dataclass(frozen=True)
class Toc:
    chapters: tuple[TocChapter, ...]
    spine: tuple[str, ...]  # archive paths in reading order
    unresolved: tuple[str, ...] = field(default=())  # entries that point at no spine item
    # sha256 of each spine item, in reading order ("" when it cannot be read): two chapters with
    # the same content under two names are one chapter copied
    spine_hashes: tuple[str, ...] = field(default=())


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _text(element: Element) -> str:
    return " ".join("".join(element.itertext()).split())


def _resolve(base: str, href: str) -> tuple[str, str] | None:
    """(archive path, fragment) of `href` read from the document at `base`, or None when it
    leaves the archive or is not a plain relative reference."""
    parsed = urllib.parse.urlsplit(href.strip())
    if parsed.scheme or parsed.netloc or not parsed.path:
        return None
    path = urllib.parse.unquote(parsed.path)
    if path.startswith("/") or "\x00" in path or "\\" in path:
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), path))
    if joined.startswith("../") or joined == "..":
        return None
    return joined, urllib.parse.unquote(parsed.fragment)


def _nav_leaves(ol: Element) -> list[tuple[str, str]]:
    """(title, href) of the leaf items of a `nav` list: a group that has a sub-list is a heading,
    not a chapter."""
    found: list[tuple[str, str]] = []
    for item in (c for c in ol if _local(c.tag) == "li"):
        nested = [c for c in item if _local(c.tag) == "ol"]
        if nested:
            for sub in nested:
                found.extend(_nav_leaves(sub))
            continue
        link = next((e for e in item.iter() if _local(e.tag) == "a"), None)
        if link is not None:
            found.append((_text(link), link.get("href") or ""))
    return found


def _nav_entries(root: Element) -> list[tuple[str, str]]:
    navs = [e for e in root.iter() if _local(e.tag) == "nav"]
    chosen = next(
        (n for n in navs if "toc" in (n.get("{http://www.idpf.org/2007/ops}type") or "").split()),
        None,
    )
    if chosen is None:
        chosen = next((n for n in navs if "toc" in (n.get("type") or "").split()), None)
    if chosen is None:
        return []
    lists = [c for c in chosen if _local(c.tag) == "ol"]
    return _nav_leaves(lists[0]) if lists else []


def _ncx_entries(root: Element) -> list[tuple[str, str]]:
    maps = [c for c in root if _local(c.tag) == "navMap"]
    if not maps:
        return []
    found: list[tuple[str, str]] = []
    for point in maps[0].iter():
        if _local(point.tag) != "navPoint":
            continue
        if any(_local(c.tag) == "navPoint" for c in point):
            continue  # a group: only its leaves are chapters
        label = next((e for e in point.iter() if _local(e.tag) == "text"), None)
        content = next((c for c in point if _local(c.tag) == "content"), None)
        if content is None:
            continue
        found.append((_text(label) if label is not None else "", content.get("src") or ""))
    return found


def _package(archive: SafeArchive) -> tuple[str, Element]:
    try:
        opf_path = find_opf_path(archive)
        return opf_path, safe_parse_xml(archive.read(opf_path, max_bytes=_MAX_DOC))
    except (UnsafeXmlError, UnsafeArchiveError, KeyError, ValueError) as error:
        raise TocError("opf_unreadable") from error


def _read(archive: SafeArchive) -> Toc:
    opf_path, package = _package(archive)
    names = {e.name for e in archive.entries}
    manifest: dict[str, tuple[str, str, str]] = {}  # id -> (path, media type, properties)
    for item in package.iter():
        if _local(item.tag) != "item" or not item.get("id") or not item.get("href"):
            continue
        resolved = _resolve(opf_path, item.get("href", ""))
        if resolved is not None and resolved[0] in names:
            manifest[str(item.get("id"))] = (
                resolved[0], item.get("media-type", ""), item.get("properties", ""),
            )  # fmt: skip
    spine_ids = [
        str(i.get("idref")) for i in package.iter() if _local(i.tag) == "itemref" and i.get("idref")
    ]
    spine = tuple(manifest[i][0] for i in spine_ids if i in manifest)
    index: dict[str, int] = {}
    for place, path in enumerate(spine):
        index.setdefault(path, place)  # the first place of an item wins
    nav = next((p for p, _t, props in manifest.values() if "nav" in props.split()), None)
    ncx_id = next((str(s.get("toc")) for s in package.iter() if _local(s.tag) == "spine"), None)
    ncx = manifest[ncx_id][0] if ncx_id in manifest else None
    ncx = ncx or next((p for p, t, _ in manifest.values() if t == _NCX_TYPE), None)
    raw: list[tuple[str, str]] = []
    base = ""
    try:
        if nav is not None:
            base = nav
            raw = _nav_entries(safe_parse_xml(archive.read(nav, max_bytes=_MAX_DOC)))
        if not raw and ncx is not None:
            base = ncx
            raw = _ncx_entries(safe_parse_xml(archive.read(ncx, max_bytes=_MAX_DOC)))
    except (UnsafeXmlError, UnsafeArchiveError, KeyError) as error:
        raise TocError("toc_unreadable") from error
    if nav is None and ncx is None:
        raise TocError("no_toc")
    chapters: list[TocChapter] = []
    unresolved: list[str] = []
    for title, href in raw:
        resolved = _resolve(base, href)
        if resolved is None or resolved[0] not in index:
            unresolved.append(href)
            continue
        chapters.append(TocChapter(title, resolved[0], resolved[1], index[resolved[0]]))
    hashes = []
    for path in spine:
        try:
            hashes.append(hashlib.sha256(archive.read(path, max_bytes=_MAX_DOC)).hexdigest())
        except (UnsafeArchiveError, KeyError, ValueError):
            hashes.append("")
    return Toc(tuple(chapters), spine, tuple(unresolved), tuple(hashes))


def read_toc(source: Path | BinaryIO, limits: ArchiveLimits = DEFAULT_LIMITS) -> Toc:
    """The chapters of the table of contents of the EPUB `source`, in document order."""
    try:
        with safe_open_archive(source, name="book.epub", limits=limits) as archive:
            return _read(archive)
    except UnsafeArchiveError as error:
        raise TocError(f"unsafe_archive:{error.reason}") from None
