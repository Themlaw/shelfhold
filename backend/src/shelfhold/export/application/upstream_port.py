"""What the purge asks of the connected service (EF-BIB-052, EF-BIB-053, EF-SRC-033).

The purge is an export-side orchestration that holds no client of the service (the layers above
it build one): it asks through this port. The adapter lives in `shelfhold/purge_upstream.py` and
goes by the road of "Mettre à l'abri" (`UpstreamPurge`): the intent that names the request, the
service asked first, the erasure sent once and never again.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Protocol

from shelfhold.library.public import DownloadTicket

__all__ = [
    "ALREADY_GONE",
    "CANCELLED",
    "KEPT_BY_OTHER_ACCOUNT",
    "RELEASED",
    "SeriesKey",
    "UnfollowResult",
    "UpstreamPort",
    "UpstreamResult",
]

RELEASED: Final = "released"
ALREADY_GONE: Final = "gone"
CANCELLED: Final = "cancelled"
KEPT_BY_OTHER_ACCOUNT: Final = "kept_by_other_account"
SeriesKey = tuple[str, str, str]  # (plugin id, source key, remote id): never a title


@dataclass(frozen=True)
class UpstreamResult:
    """The fate of one download request. `outcome` is one of the four above, or `refused`
    (`reason` is a code of the rule: the service is paused, its roles unknown, the source not
    loaded, another deed open), `unresolved` (a deed was begun, nobody knows) or `unavailable`
    (the service did not answer: nothing was sent that we know of)."""

    request_id: str
    outcome: str
    reason: str = ""
    freed_bytes: int | None = None


@dataclass(frozen=True)
class UnfollowResult:
    key: SeriesKey
    done: bool
    reason: str = ""  # a code, never a message of the service


class UpstreamPort(Protocol):
    def release(self, tickets: Sequence[DownloadTicket]) -> list[UpstreamResult]:
        """Let the service go of each download, once. Never raises for one request."""
        ...

    def unfollow(self, series: Sequence[SeriesKey]) -> list[UnfollowResult]:
        """Take each series out of the service's library (EF-SRC-029). Idempotent."""
        ...

    def holds(self, tickets: Sequence[DownloadTicket]) -> dict[str, bool]:
        """Read only: for each request, whether the service still holds the downloaded chapter.
        A request the service cannot answer for is left out."""
        ...

    def followed(self) -> set[tuple[str, str]] | None:
        """Read only: `(source key, remote id)` of the series in the service's library; None when
        the service cannot answer."""
        ...
