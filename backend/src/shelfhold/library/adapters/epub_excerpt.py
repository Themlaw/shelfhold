"""The entries an excerpt of an EPUB may serve (M5 L05a, ADR-0055 D-P1, « Liste fermée »).

Computed here, from the archive the server is about to read, with `safe_parse_xml` and never a name
from a caller: the first documents of the reading order within the bound (`spine_excerpt`), the
package and the container, the navigation document, and the resources those documents reference
(images, styles, fonts, `@import` and `url()` chains included). A link `<a href>` to another chapter
is not a resource and is never followed.

A file enters the list as a **resource** only if the manifest of the package types it as an image
(an SVG document excepted), a font or a style sheet: never a document (XHTML, HTML, SVG), never a
type the manifest does not name, whatever the attribute that points at it. And the resources
together stay within `EXCERPT_RATIO` of the archive: one that does not fit is not served, the
others are. A document that cannot be read, a package without a usable reading order, or a first
document beyond the bound: `None`, « extrait indisponible ».
"""

from __future__ import annotations

import posixpath
import re
from typing import Final
from urllib.parse import unquote, urlsplit

from shelfhold.infrastructure.archives import SafeArchive, UnsafeArchiveError
from shelfhold.infrastructure.packaging.epub.opf import OpfError, find_opf_path
from shelfhold.infrastructure.xml import Element, UnsafeXmlError, safe_parse_xml
from shelfhold.reading.domain.excerpt import EXCERPT_RATIO, spine_excerpt

__all__ = ["epub_excerpt"]

_XLINK: Final = "{http://www.w3.org/1999/xlink}href"
_DOCUMENT_TYPES: Final = frozenset({"application/xhtml+xml", "text/html"})
_URL = re.compile(r"""url\(\s*['"]?([^'")\s]+)""", re.IGNORECASE)
_IMPORT = re.compile(r"""@import\s+['"]([^'"]+)""", re.IGNORECASE)
_RESOURCE_ATTRIBUTES: Final = ("src", "poster", "data", _XLINK)
_MAX_ROUNDS = 8  # `@import` chains: bounded, never a loop
_FONT_TYPES: Final = frozenset(
    {"application/vnd.ms-opentype", "application/font-woff", "application/font-sfnt"}
)


def _is_resource_type(kind: str) -> bool:
    """An image (not an SVG document), a font or a style sheet, by the manifest's word."""
    kind = kind.split(";", 1)[0].strip().lower()
    if kind == "image/svg+xml":
        return False
    return (
        kind.startswith(("image/", "font/", "application/x-font-"))
        or kind in _FONT_TYPES
        or kind == "text/css"
    )


def _local(tag: object) -> str:
    return str(tag).rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def _resolve(base: str, raw: str, names: frozenset[str]) -> str | None:
    value = raw.strip()
    parts = urlsplit(value)
    if not value or parts.scheme or parts.netloc or value.startswith(("/", "#", "data:")):
        return None
    joined = posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(parts.path)))
    return joined if joined in names else None


def _css_references(text: str) -> list[str]:
    return [*_URL.findall(text), *_IMPORT.findall(text)]


def _document_references(root: Element) -> list[str]:
    found: list[str] = []
    for element in root.iter():
        local = _local(element.tag)
        if local == "style" and element.text:
            found += _css_references(element.text)
        if "style" in element.attrib:
            found += _css_references(str(element.attrib["style"]))
        for name in _RESOURCE_ATTRIBUTES:
            if name in element.attrib:
                found.append(str(element.attrib[name]))
        if "srcset" in element.attrib:
            found += [c.split()[0] for c in str(element.attrib["srcset"]).split(",") if c.strip()]
        if local == "link" and "href" in element.attrib:
            found.append(str(element.attrib["href"]))
        if local in {"image", "use"} and "href" in element.attrib:  # SVG 2, not an `<a>`
            found.append(str(element.attrib["href"]))
    return found


def epub_excerpt(archive: SafeArchive) -> frozenset[str] | None:
    names = frozenset(e.name for e in archive.entries if not e.is_dir)
    sizes = {e.name: e.size for e in archive.entries}
    try:
        opf_path = find_opf_path(archive)
        package = safe_parse_xml(archive.read(opf_path))
        manifest: dict[str, tuple[str, str, str]] = {}
        for item in package.iter():
            if _local(item.tag) == "item" and item.get("id") and item.get("href"):
                entry = _resolve(opf_path, str(item.get("href")), names)
                if entry is not None:
                    manifest[str(item.get("id"))] = (
                        entry,
                        str(item.get("media-type", "")),
                        str(item.get("properties", "")),
                    )
        spine = list(  # distinct documents: one repeated in the reading order is still one
            dict.fromkeys(
                manifest[str(ref.get("idref"))][0]
                for ref in package.iter()
                if _local(ref.tag) == "itemref"
                and str(ref.get("idref")) in manifest
                and manifest[str(ref.get("idref"))][1] in _DOCUMENT_TYPES
            )
        )
        kinds = {entry: kind for entry, kind, _p in manifest.values()}
        chosen = spine_excerpt([(entry, sizes.get(entry, 0)) for entry in spine])
        if chosen is None:
            return None
        allowed = {opf_path, *chosen}
        if "META-INF/container.xml" in names:
            allowed.add("META-INF/container.xml")
        for entry, kind, properties in manifest.values():  # the way into the book
            if "nav" in properties.split() or kind == "application/x-dtbncx+xml":
                allowed.add(entry)
        budget = int(sum(sizes.values()) * EXCERPT_RATIO)  # resources, all together
        pending = [*chosen]
        for _ in range(_MAX_ROUNDS):
            following: list[str] = []
            for entry in pending:
                if kinds.get(entry, "").lower() == "text/css":
                    references = _css_references(archive.read(entry).decode("utf-8", "replace"))
                elif entry in chosen:
                    references = _document_references(safe_parse_xml(archive.read(entry)))
                else:
                    continue
                for raw in references:
                    target = _resolve(entry, raw, names)
                    if (
                        target is None
                        or target in allowed
                        or not _is_resource_type(kinds.get(target, ""))
                        or sizes.get(target, 0) > budget
                    ):
                        continue  # not a resource, not typed as one, or beyond the bound
                    budget -= sizes.get(target, 0)
                    allowed.add(target)
                    following.append(target)
            if not following:
                break
            pending = following
        return frozenset(allowed)
    except (OpfError, UnsafeXmlError, UnsafeArchiveError, KeyError, ValueError):
        return None
