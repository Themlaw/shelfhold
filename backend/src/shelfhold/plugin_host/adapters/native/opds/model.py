"""What a feed says, as plain data (EF-SRC-006): the same shapes for OPDS 1.2 and 2.0.

Nothing here is trusted: a catalogue is a third party's. Text is kept as received (the host
sanitizes it before it is shown), a link is kept only when it is an http(s) address without
credentials, and the number of entries, links and characters is bounded.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Final, Literal
from urllib.parse import urljoin, urlsplit

from shelfhold.sdk.source import SourceChanged

__all__ = [
    "ACCEPTED_TYPES",
    "MAX_ENTRIES",
    "MAX_LINKS",
    "MAX_TEXT",
    "Entry",
    "Feed",
    "FeedError",
    "Link",
    "absolute",
    "digest_of",
    "media_type",
    "origin_of",
]

MAX_ENTRIES: Final = 500  # entries read from one page of a feed
MAX_LINKS: Final = 64  # links read from one entry or one feed
MAX_TEXT: Final = 20_000  # characters kept of one text field

# The file types the host takes (`catalog` announces the same list): the bytes decide what a file
# is, this only says which links are worth offering.
ACCEPTED_TYPES: Final = frozenset(
    {
        "application/epub+zip",
        "application/vnd.comicbook+zip",
        "application/x-cbz",
        "application/zip",
    }
)
ACQUISITION_PREFIX: Final = "http://opds-spec.org/acquisition"
FREE_RELS: Final = frozenset({ACQUISITION_PREFIX, f"{ACQUISITION_PREFIX}/open-access"})
LOCKED_RELS: Final = frozenset(
    {f"{ACQUISITION_PREFIX}/buy", f"{ACQUISITION_PREFIX}/borrow", f"{ACQUISITION_PREFIX}/subscribe"}
)
_DEFAULT_PORTS: Final = {"http": 80, "https": 443}


class FeedError(SourceChanged):
    """The document is not one this reader takes: malformed, hostile, or not OPDS. The message is
    a closed reason, never a piece of the document or of its address."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Link:
    href: str  # absolute, http(s), no credentials
    rel: str = ""
    type: str = ""
    title: str = ""
    templated: bool = False


@dataclass(frozen=True)
class Entry:
    id: str
    title: str
    summary: str = ""
    authors: tuple[str, ...] = ()
    language: str = ""
    acquisitions: tuple[Link, ...] = ()  # the free files worth offering, best first
    subsection: str | None = None  # another feed, for a navigation entry
    locked: bool = False  # offered, but to buy or to borrow: not a free file
    digest: str = ""  # sha256 of the entry as the source wrote it
    links: tuple[Link, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class Feed:
    kind: Literal["atom", "opds2"]
    title: str
    entries: tuple[Entry, ...]
    next: str | None = None
    search: Link | None = None
    is_entry: bool = False  # an entry document served alone


def media_type(raw: str | None) -> str:
    """`type;params` -> `type`, lower-cased."""
    return (raw or "").split(";", 1)[0].strip().lower()


def absolute(base: str, href: str | None) -> str | None:
    """`href` resolved against `base`, or None when it is not a plain http(s) address: another
    scheme (`file:`, `javascript:`, `data:`), a missing host, credentials in the address."""
    if not href or len(href) > 4096 or any(ord(c) < 32 for c in href):
        return None
    try:
        joined = urljoin(base, href.strip())
        parts = urlsplit(joined)
        host = parts.hostname
        _ = parts.port  # a bad port raises here
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not host or "@" in parts.netloc:
        return None
    return parts._replace(fragment="").geturl()


def origin_of(url: str) -> tuple[str, str, int] | None:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS or not host:
        return None
    return parts.scheme, host, port or _DEFAULT_PORTS[parts.scheme]


def digest_of(canonical: str) -> str:
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8", "surrogatepass")).hexdigest()
