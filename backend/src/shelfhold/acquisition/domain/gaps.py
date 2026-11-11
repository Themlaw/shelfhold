"""Numbering gaps beyond the watermark (EF-BIB-056, I20).

A gap is a whole place that is absent between two places that are present, on the same track and
in the same segment, and beyond the follow watermark. Decimal numbers and parts never make one:
10, 10.5 and 11 have no hole, and 10.5 does not fill 11 either. What is below the watermark is not
looked at here (`backfill --gaps` is the explicit action). Pure: the facts come from the caller.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from shelfhold.acquisition.domain.watermark import Watermark

__all__ = ["MAX_GAPS", "Gap", "Place", "find_gaps"]

MAX_GAPS: Final = 200  # a renumbering that spans thousands of places names the first ones only


@dataclass(frozen=True)
class Place:
    """A numbered Unit that is known: listed by a binding, or held."""

    track: str
    segment: str
    ordinal: int
    number: Decimal


@dataclass(frozen=True, order=True)
class Gap:
    track: str
    ordinal: int
    segment: str
    number: int


def find_gaps(
    places: Iterable[Place], mark: Watermark | None, acknowledged: frozenset[tuple[str, str, str]]
) -> list[Gap]:
    """`acknowledged`: `(track, segment, number_norm)` the admin said do not exist.

    Without a watermark there is no "beyond": nothing is a gap yet.
    """
    if mark is None:
        return []
    by_run: dict[tuple[str, int, str], set[Decimal]] = {}
    for place in places:
        by_run.setdefault((place.track, place.ordinal, place.segment), set()).add(place.number)
    found: list[Gap] = []
    for (track, ordinal, segment), numbers in sorted(by_run.items()):
        if ordinal < mark.ordinal:
            continue  # an older segment is entirely below the watermark
        first = int(min(numbers).to_integral_value(rounding="ROUND_FLOOR")) + 1
        last = int(max(numbers).to_integral_value(rounding="ROUND_CEILING")) - 1
        if ordinal == mark.ordinal:
            first = max(first, int(mark.number.to_integral_value(rounding="ROUND_FLOOR")) + 1)
        for n in range(first, last + 1):
            if len(found) >= MAX_GAPS:
                return found
            if Decimal(n) in numbers or (track, segment, str(n)) in acknowledged:
                continue
            found.append(Gap(track, ordinal, segment, n))
    return found
