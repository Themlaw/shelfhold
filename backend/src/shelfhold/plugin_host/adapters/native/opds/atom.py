"""OPDS 1.2 (Atom) reader (EF-SRC-006).

The XML is parsed by the host's hardened parser, handed to the source (an adapter imports nothing
of the core, ENF-SEC-014): no DTD, no entity, bounded size, depth and element count. What comes out
is plain data (`model.Feed`); the text is kept as received.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from shelfhold.plugin_host.adapters.native.opds.model import (
    ACCEPTED_TYPES,
    FREE_RELS,
    LOCKED_RELS,
    MAX_ENTRIES,
    MAX_LINKS,
    MAX_TEXT,
    Entry,
    Feed,
    FeedError,
    Link,
    absolute,
    digest_of,
    media_type,
)

__all__ = ["MAX_ENTRIES", "ParseXml", "parse_atom"]

ParseXml = Callable[[bytes], Any]
ATOM = "{http://www.w3.org/2005/Atom}"
_OPDS_PROFILE = "profile=opds-catalog"


def _local(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1]


def _text(element: Any) -> str:
    return (element.text or "")[:MAX_TEXT]


def parse_atom(body: bytes, url: str, parse_xml: ParseXml) -> Feed:
    """The feed (or the single entry) of an Atom document served at `url`."""
    try:
        root = parse_xml(body)
    except (ValueError, TypeError, RecursionError):
        raise FeedError("malformed_xml") from None
    name = str(root.tag)
    if name == f"{ATOM}entry":
        return Feed("atom", "", (_entry(root, url),), is_entry=True)
    if name != f"{ATOM}feed":
        raise FeedError("not_atom")
    title = ""
    entries: list[Entry] = []
    links: list[Link] = []
    for child in root:
        local = _local(child.tag)
        if local == "title" and not title:
            title = _text(child)
        elif local == "entry":
            if len(entries) < MAX_ENTRIES:
                entries.append(_entry(child, url))
        elif local == "link" and len(links) < MAX_LINKS:
            found = _link(child, url)
            if found is not None:
                links.append(found)
    following = next((k.href for k in links if k.rel == "next"), None)
    search = next((k for k in links if k.rel == "search"), None)
    return Feed("atom", title, tuple(entries), following, search)


def _link(element: Any, base: str) -> Link | None:
    attrs = element.attrib
    href = absolute(base, attrs.get("href"))
    if href is None:
        return None
    return Link(
        href=href,
        rel=attrs.get("rel", "")[:256],
        type=attrs.get("type", "")[:256],
        title=attrs.get("title", "")[:MAX_TEXT],
        templated="{" in attrs.get("href", ""),
    )


def _is_navigation(link: Link) -> bool:
    kind = link.type.lower()
    if _OPDS_PROFILE not in kind and not kind.startswith("application/atom+xml"):
        return False
    return link.rel in ("subsection", "alternate", "") or link.rel.endswith("/sort/new")


def _entry(element: Any, base: str) -> Entry:
    ident = title = summary = language = ""
    authors: list[str] = []
    links: list[Link] = []
    for child in element:
        local = _local(child.tag)
        if local == "id" and not ident:
            ident = _text(child).strip()
        elif local == "title" and not title:
            title = _text(child)
        elif local in ("summary", "content") and not summary:
            summary = _text(child)
        elif local == "language" and not language:
            language = _text(child).strip()[:35]
        elif local == "author":
            for part in child:
                if _local(part.tag) == "name" and _text(part).strip():
                    authors.append(_text(part))
        elif local == "link" and len(links) < MAX_LINKS:
            found = _link(child, base)
            if found is not None:
                links.append(found)
    free = [k for k in links if k.rel in FREE_RELS and media_type(k.type) in ACCEPTED_TYPES]
    free.sort(key=lambda k: 0 if media_type(k.type) == "application/epub+zip" else 1)
    locked = not free and any(
        k.rel in LOCKED_RELS and media_type(k.type) in ACCEPTED_TYPES for k in links
    )
    section = None if free else next((k.href for k in links if _is_navigation(k)), None)
    return Entry(
        id=ident, title=title, summary=summary, authors=tuple(authors), language=language,
        acquisitions=tuple(free), subsection=section, locked=locked,
        digest=digest_of(_canonical(element)), links=tuple(links),
    )  # fmt: skip


def _canonical(element: Any) -> str:
    """A deterministic rendering of the element and what is under it: the tag, the attributes in
    order, the text as received. The text around the element and the namespace declarations
    are not part of it, so the same entry has the same digest in a feed and served alone."""
    parts: list[str] = []
    _walk(element, parts)
    return "".join(parts)


def _walk(element: Any, out: list[str]) -> None:
    out.append(f"<{element.tag}")
    for key in sorted(element.attrib):
        out.append(f" {key}={element.attrib[key]!r}")
    out.append(">")
    children = list(element)
    text = element.text or ""
    if text.strip() or not children:
        out.append(f"{text!r}")
    for child in children:
        _walk(child, out)
    out.append(f"</{element.tag}>")
