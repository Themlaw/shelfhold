"""The Atom writer lives in `infrastructure.atom_feed` (no user-facing text here)."""

from shelfhold.infrastructure.atom_feed import (
    ATOM,
    ENTRY_TYPE,
    FEED_TYPE,
    OPDS,
    OPENSEARCH,
    Entry,
    Kind,
    Link,
    build_feed,
    feed_type,
    opensearch_description,
)

__all__ = [
    "ATOM",
    "ENTRY_TYPE",
    "FEED_TYPE",
    "OPDS",
    "OPENSEARCH",
    "Entry",
    "Kind",
    "Link",
    "build_feed",
    "feed_type",
    "opensearch_description",
]
