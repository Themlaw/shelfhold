"""The OPDS 2.0 JSON feed (M5 L19, EF-INT-007, ADR-0006): the same feed as the Atom one, written
as JSON.

Nothing is queried here. The routes of `routes.py` are registered twice, once per `FeedFormat`,
and each handler builds one `FeedDoc` from the rows the profile may see (the one filter of the
catalogue, read from the profile of the token); this module only writes that document. So an
entry of the 2.0 feed is an entry of the 1.2 feed, by construction: it has the same identifier,
title, links and counts, behind the same token, with the same 404 for what is hidden or absent.

A JSON document is built from data and dumped by `json.dumps`, never concatenated: a title that
holds a quote or a bracket is a string, nothing else. The hrefs are those of `links.Paths`: the
feeds under `/opds/v2`, and the files, covers and pages under `/opds` (one route each, shared).
"""

import json
from typing import Any

from shelfhold.interfaces.http_api.opds.feed import (
    Entry,
    FeedDoc,
    Kind,
    Link,
)
from shelfhold.interfaces.http_api.opds.links import Paths

__all__ = ["JSON_TYPE", "JsonFeed", "render_json"]

JSON_TYPE = "application/opds+json"
_IMAGE_RELS = {"http://opds-spec.org/image", "http://opds-spec.org/image/thumbnail"}
_BOOK = "http://schema.org/Book"


def _link(link: Link) -> dict[str, Any]:
    out: dict[str, Any] = {"rel": link.rel, "href": link.href}
    if link.type:
        out["type"] = link.type
    if link.title:
        out["title"] = link.title
    if "{" in link.href:  # a URI template: the client fills it (`{pageNumber}`, `{?q}`)
        out["templated"] = True
    if link.length is not None:
        out["length"] = link.length
    if link.pse_count is not None:
        out["properties"] = {"numberOfItems": link.pse_count}
    return out


def _image(link: Link) -> dict[str, Any]:
    return {"href": link.href, **({"type": link.type} if link.type else {})}


def _navigation(entry: Entry) -> dict[str, Any]:
    """An entry of a navigation feed: a door, that is one link and a title."""
    door = entry.links[0]
    item = _link(door)
    item["title"] = entry.title
    item["properties"] = {"identifier": entry.id}
    return item


def _publication(entry: Entry) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "@type": _BOOK,
        "identifier": entry.id,
        "title": entry.title,
        "modified": entry.updated,
    }
    if entry.summary:
        metadata["description"] = entry.summary
    out: dict[str, Any] = {
        "metadata": metadata,
        "links": [_link(x) for x in entry.links if x.rel not in _IMAGE_RELS],
    }
    images = [_image(x) for x in entry.links if x.rel in _IMAGE_RELS]
    if images:
        out["images"] = images
    return out


def render_json(doc: FeedDoc) -> str:
    metadata: dict[str, Any] = {
        "identifier": doc.feed_id,
        "title": doc.title,
        "modified": doc.updated,
    }
    if doc.total is not None:  # the number of rows the reader may see, never a hidden one
        metadata["numberOfItems"] = doc.total
        if doc.per_page:
            metadata["itemsPerPage"] = doc.per_page
            metadata["currentPage"] = doc.start // doc.per_page + 1
    body: dict[str, Any] = {"metadata": metadata, "links": [_link(x) for x in doc.links]}
    if doc.kind == "navigation":
        body["navigation"] = [_navigation(e) for e in doc.entries]
    else:
        body["publications"] = [_publication(e) for e in doc.entries]
    return json.dumps(body, ensure_ascii=True, separators=(",", ":"))


class JsonFeed:
    """The OPDS 2.0 way of writing a feed (see `feed.FeedFormat`)."""

    op_prefix = "opds2"
    mount = "/v2"
    root_media = JSON_TYPE

    def media(self, kind: Kind) -> str:
        del kind  # a JSON feed is one type; navigation and publications are told by their keys
        return JSON_TYPE

    def render(self, doc: FeedDoc) -> str:
        return render_json(doc)

    def paths(self, prefix: str) -> Paths:
        return Paths(prefix, json_feed=True)

    def start_links(self, paths: Paths) -> list[Link]:
        return [
            Link("start", paths.root(), JSON_TYPE),
            Link("search", paths.search_template(), JSON_TYPE),
        ]
