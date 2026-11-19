"""What a feed is, whatever the way it is written (no user-facing text here).

A route builds a `FeedDoc` from the rows the profile may see, then one `FeedFormat` writes it: the
Atom writer lives in `platform.atom_feed`, the JSON one in `opds.v2`. There is one set of queries
and two ways to write their result (EF-INT-007, M5 L19).
"""

from dataclasses import dataclass
from typing import Protocol

from shelfhold.interfaces.http_api.opds.links import Paths
from shelfhold.platform.atom_feed import (
    ATOM,
    ENTRY_TYPE,
    FEED_TYPE,
    OPDS,
    OPENSEARCH,
    PSE,
    Entry,
    Kind,
    Link,
    build_feed,
    feed_type,
    opensearch_description,
)


@dataclass(frozen=True)
class FeedDoc:
    """One feed, as the routes build it. `total` is the number of rows the reader may see."""

    feed_id: str
    title: str
    updated: str
    kind: Kind
    links: list[Link]
    entries: list[Entry]
    total: int | None = None
    start: int = 0
    per_page: int | None = None


class FeedFormat(Protocol):
    """A way to write a `FeedDoc`: the routes ask it for the media type, the hrefs of the feeds
    and the links of the start of a feed, and nothing else differs."""

    op_prefix: str  # the prefix of the operation identifiers
    mount: str  # where the feed routes sit under `/opds`
    root_media: str  # the media type of the feed routes, for the schema

    def media(self, kind: Kind) -> str: ...

    def render(self, doc: FeedDoc) -> str: ...

    def paths(self, prefix: str) -> Paths: ...

    def start_links(self, paths: Paths) -> list[Link]: ...


__all__ = [
    "ATOM",
    "ENTRY_TYPE",
    "FEED_TYPE",
    "OPDS",
    "OPENSEARCH",
    "PSE",
    "Entry",
    "FeedDoc",
    "FeedFormat",
    "Kind",
    "Link",
    "build_feed",
    "feed_type",
    "opensearch_description",
]
