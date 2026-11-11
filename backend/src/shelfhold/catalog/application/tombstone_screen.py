"""What the catalog asks of the tombstones (EF-BIB-053, EF-BIB-054 (4), EF-BIB-049).

The tombstones live in the library context and are sealed; the catalog never reads them. It asks
two questions in plain values and gets the blocked ones back: which places would the Units of a
merged Edition take, and which external identifiers of a Work are tombstoned. The answers decide
before anything is written; a blocked row is never deleted, only kept out.

The screen is **required** wherever an operation could bring a purged thing back: a caller that
has none is a caller that must say so by passing a screen that blocks nothing (tests do).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from shelfhold.catalog.domain.external_ids import ExternalId

__all__ = ["MergedPlace", "TombstoneScreen", "TombstoneScreenMissingError"]


class TombstoneScreenMissingError(RuntimeError):
    """An operation that could bring back a purged Work or place was built without a screen."""


@dataclass(frozen=True)
class MergedPlace:
    """A Unit as the surviving Edition would hold it: the raw values a `place` tombstone cites."""

    unit_id: str
    edition_key: tuple[tuple[str, str], ...]  # raw key of the surviving Edition (sentinels kept)
    track: str
    number_segment: str
    number_raw: str
    part: str


class TombstoneScreen(Protocol):
    def places(self, places: Sequence[MergedPlace]) -> set[str]:
        """The `unit_id`s whose place a tombstone covers."""
        ...

    def work_ids(self, ids: Sequence[ExternalId]) -> set[tuple[str, str]]:
        """The `(provider, value)` of the Work identifiers a `work` tombstone covers."""
        ...
