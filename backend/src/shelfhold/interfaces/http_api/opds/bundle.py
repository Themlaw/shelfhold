"""The extension point of the block of a series (ADR-0053 §2, §3, §8; L49a plugs in here).

The route decides who may ask, and nothing a provider returns reaches the reader before that
decision:

- the Edition is one the reader's scope may see, read from the database at this very request;
- a provider is never asked about an Edition the reader may not see (so a feed, a count or a
  header it builds never mentions one);
- the file name a provider gives is cleaned by the route (no path, no control character).

A provider still re-checks every chapter of the block for the profile (§8): the route only keeps
the door honest, it does not know what a block is made of. `open` receives the part (`(2 sur 3)`)
and the id of the token, so that the provider can stop a transfer when the token is revoked and
limit the transfers of one token. It may raise `BundleNotFoundError` (an absent thing),
`BundleGoneError` (410) and `BundleBusyError` (429).
"""

from typing import Any, Protocol

from shelfhold.reading.bundle import (
    DEFAULT_MEDIA_TYPE,
    BundleBusyError,
    BundleDownload,
    BundleGoneError,
    BundleNotFoundError,
    BundleOffer,
)

__all__ = [
    "DEFAULT_MEDIA_TYPE",
    "BundleBusyError",
    "BundleDownload",
    "BundleGoneError",
    "BundleNotFoundError",
    "BundleOffer",
    "SeriesBundleProvider",
]


class SeriesBundleProvider(Protocol):
    """`scope` is the reader's (`ReaderScope`); `user_id` the profile of the token."""

    def offers(self, *, user_id: str, scope: Any, edition_id: str) -> list[BundleOffer]: ...

    def open(
        self, *, user_id: str, scope: Any, edition_id: str, part: int = 1,
        token_id: str | None = None,
    ) -> BundleDownload: ...  # fmt: skip
