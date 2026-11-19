"""The Atom document of an OPDS 1.2 feed (ADR-0053 §1). The XML is written here, never parsed (the
only parser is `safe_parse_xml`, ENF-SEC-014): every value is data, so it goes through `_clean`
(a character XML 1.0 cannot hold is dropped) and `escape` (markup characters become entities),
in text and in attributes alike. Nothing else is ever concatenated into the document."""

from dataclasses import dataclass, field
from html import escape
from typing import Literal

ATOM = "http://www.w3.org/2005/Atom"
OPDS = "http://opds-spec.org/2010/catalog"
OPENSEARCH = "http://a9.com/-/spec/opensearch/1.1/"
PSE = "http://vaemendis.net/opds-pse/ns"
FEED_TYPE = "application/atom+xml"
ENTRY_TYPE = "application/atom+xml;type=entry;profile=opds-catalog"

Kind = Literal["navigation", "acquisition"]


def feed_type(kind: Kind) -> str:
    return f"{FEED_TYPE};profile=opds-catalog;kind={kind}"


def _clean(text: str) -> str:
    """The characters XML 1.0 allows (tab, newline, the printable planes), nothing else."""
    return "".join(
        c
        for c in text
        if c in "\t\n\r"
        or 0x20 <= ord(c) <= 0xD7FF
        or 0xE000 <= ord(c) <= 0xFFFD
        or 0x10000 <= ord(c) <= 0x10FFFF
    )


clean_text = _clean  # the same cleaning for every way of writing a feed (the JSON one too)


@dataclass(frozen=True)
class Link:
    rel: str
    href: str
    type: str | None = None
    title: str | None = None
    length: int | None = None
    pse_count: int | None = None  # OPDS-PSE: the number of pages of a `pse:stream` link


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    updated: str
    links: list[Link] = field(default_factory=list)
    summary: str | None = None


_DECLARATION = '<?xml version="1.0" encoding="utf-8"?>\n'


def _esc(value: str) -> str:
    return escape(_clean(value), quote=True)


def _node(tag: str, value: str, **attrs: str) -> str:
    shown = "".join(f' {k}="{_esc(v)}"' for k, v in attrs.items())
    return f"<{tag}{shown}>{_esc(value)}</{tag}>"


def _link(link: Link) -> str:
    attrs = {"rel": link.rel, "href": link.href}
    if link.type:
        attrs["type"] = link.type
    if link.title:
        attrs["title"] = link.title
    if link.length is not None:
        attrs["length"] = str(link.length)
    if link.pse_count is not None:
        attrs["pse:count"] = str(link.pse_count)
    return "<link" + "".join(f' {k}="{_esc(v)}"' for k, v in attrs.items()) + "/>"


def build_feed(
    *,
    feed_id: str,
    title: str,
    updated: str,
    kind: Kind,
    links: list[Link],
    entries: list[Entry],
    total: int | None = None,
    start: int = 0,
    per_page: int | None = None,
) -> str:
    """A feed. `total` is the number of rows the reader may see (a figure that counts a hidden
    row is a leak: the caller gives the visible count or nothing)."""
    del kind  # the kind lives in the `Content-Type` and in the `type` of the links that point here
    parts = [
        f'<feed xmlns="{ATOM}" xmlns:opds="{OPDS}" xmlns:opensearch="{OPENSEARCH}"'
        f' xmlns:pse="{PSE}">',
        _node("id", feed_id),
        _node("title", title),
        _node("updated", updated),
        *(_link(link) for link in links),
    ]
    if total is not None:
        parts.append(_node("opensearch:totalResults", str(total)))
        parts.append(_node("opensearch:startIndex", str(start + 1)))
        if per_page is not None:
            parts.append(_node("opensearch:itemsPerPage", str(per_page)))
    for entry in entries:
        parts.append("<entry>")
        parts += [
            _node("id", entry.id),
            _node("title", entry.title),
            _node("updated", entry.updated),
        ]
        if entry.summary:
            parts.append(_node("summary", entry.summary, type="text"))
        parts += [_link(link) for link in entry.links]
        parts.append("</entry>")
    parts.append("</feed>")
    return _DECLARATION + "".join(parts)


def opensearch_description(*, template: str, title: str) -> str:
    """The OpenSearch description of the search of this feed: one URL template, under `/opds`."""
    url = f'<Url type="{_esc(feed_type("acquisition"))}" template="{_esc(template)}"/>'
    return (
        _DECLARATION
        + f'<OpenSearchDescription xmlns="{OPENSEARCH}">'
        + _node("ShortName", _clean(title)[:16])
        + _node("Description", title)
        + "<InputEncoding>UTF-8</InputEncoding>"
        + url
        + "</OpenSearchDescription>"
    )
