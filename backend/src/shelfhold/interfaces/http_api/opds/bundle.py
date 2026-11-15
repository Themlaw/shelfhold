"""The extension point of the block of a series (ADR-0053 §2, §3, §8; lot L49a plugs in here).

L48 serves the navigation and offers a download only when a provider says it has one. The route
decides who may ask, and nothing a provider returns reaches the reader before that decision:

- the Edition is one the reader's scope may see, read from the database at this very request;
- a provider is never asked about an Edition the reader may not see (so a feed, a count or a
  header it builds never mentions one);
- the file name a provider gives is cleaned by the route (no path, no control character).

A provider still re-checks every member of the block for the profile (§8): the route only keeps
the door honest, it does not know what a block is made of.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol

DEFAULT_MEDIA_TYPE = "application/vnd.comicbook+zip"


@dataclass(frozen=True)
class BundleOffer:
    """What the feed says of a block, without opening a file: the name the reader will get, the
    exact size, and one visible line (for instance the chapters left out)."""

    filename: str
    size: int
    media_type: str = DEFAULT_MEDIA_TYPE
    notice: str | None = None


@dataclass(frozen=True)
class BundleDownload:
    """A block ready to stream: every file already opened and checked, `size` exact."""

    filename: str
    size: int
    chunks: Iterable[bytes]
    media_type: str = DEFAULT_MEDIA_TYPE


class SeriesBundleProvider(Protocol):
    """`scope` is the reader's (`ReaderScope`); `user_id` the profile of the token."""

    def offer(self, *, user_id: str, scope: Any, edition_id: str) -> BundleOffer | None: ...

    def open(self, *, user_id: str, scope: Any, edition_id: str) -> BundleDownload | None: ...
