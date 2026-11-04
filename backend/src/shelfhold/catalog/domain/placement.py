"""Places, Variants and revisions (EF-BIB-034, EF-BIB-050; invariants I1, I3, I4, I32, I34).

A Unit is a place `(edition, track, segment, number, part)`; a Variant is what one binding
(and one Team) gives for it, identified by `(binding, remote_unit_id)`. These functions decide,
without touching storage, what an incoming release becomes.
"""

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from shelfhold.catalog.domain.numbering import Track, UnitNumber

NUMBERING_CONFLICT = "numbering_conflict"


@dataclass(frozen=True)
class Place:
    track: str
    segment: str
    number_norm: str
    part: str  # '' is the first part: "10" and "10 part 1" are one place
    source_order: int

    @property
    def unnumbered(self) -> bool:
        return self.track == Track.UNNUMBERED


def place_of(number: UnitNumber) -> Place:
    """A number the normalizer could not read is unnumbered, even if a track word was found."""
    track = number.track if number.norm else Track.UNNUMBERED
    part = "" if number.part == 1 else str(number.part)
    return Place(str(track), number.segment, number.norm, part, number.source_order)


class IngestOutcome(StrEnum):
    NEW_VARIANT = "new_variant"
    REVISION = "revision"
    UNCHANGED = "unchanged"
    NUMBERING_CONFLICT = "numbering_conflict"
    SUPERSEDING = "superseding"


@dataclass(frozen=True)
class VariantFacts:
    id: str
    binding_id: str
    team_id: str | None
    first_seen_at: datetime
    removed: bool = False
    rank: int | None = None  # position of its Team in the Edition's priority list
    in_conflict: bool = False  # named by an open numbering_conflict suggestion

    @property
    def age(self) -> tuple[datetime, str]:
        return (self.first_seen_at, self.id)


@dataclass(frozen=True)
class Decision:
    outcome: IngestOutcome
    other_id: str | None = None  # the Variant in conflict, or the one superseded


def decide_new_variant(
    *, binding_id: str, team_id: str | None, at_place: Sequence[VariantFacts]
) -> Decision:
    """What an unknown `(binding, remote_unit_id)` becomes on a place (EF-BIB-050 points 1 and 2).

    A distinct remote id is always a new Variant, never a revision. Another live Variant of the
    same binding *and* the same Team on that place is a numbering conflict. A removed one is
    replaced: the new Variant supersedes it. Other bindings or other Teams share the place.
    """
    same = [v for v in at_place if v.binding_id == binding_id and v.team_id == team_id]
    if live := [v for v in same if not v.removed]:
        return Decision(IngestOutcome.NUMBERING_CONFLICT, min(live, key=lambda v: v.age).id)
    if gone := [v for v in same if v.removed]:
        return Decision(IngestOutcome.SUPERSEDING, max(gone, key=lambda v: v.age).id)
    return Decision(IngestOutcome.NEW_VARIANT)


def place_owner(variants: Iterable[VariantFacts]) -> str | None:
    """I4: the oldest Variant keeps the place (live ones first)."""
    pool = list(variants)
    live = [v for v in pool if not v.removed]
    chosen = live or pool
    return min(chosen, key=lambda v: v.age).id if chosen else None


def choose_preferred(variants: Iterable[VariantFacts]) -> str | None:
    """The one Variant exported for a Unit (I3): Team priority, then the oldest live one.

    A Variant in an open numbering conflict is never chosen: nothing is exported for it without
    the admin's decision (EF-BIB-050).
    """
    live = [v for v in variants if not v.removed and not v.in_conflict]
    if not live:
        return None
    return min(live, key=lambda v: (v.rank is None, v.rank or 0, *v.age)).id


def missing_numbers(numbers: Iterable[tuple[str, Decimal]]) -> list[tuple[str, str]]:
    """Whole numbers absent between the first and the last of each segment (backfill --gaps)."""
    by_segment: dict[str, set[int]] = {}
    for segment, value in numbers:
        if value == value.to_integral_value():
            by_segment.setdefault(segment, set()).add(int(value))
    holes: list[tuple[str, str]] = []
    for segment, seen in sorted(by_segment.items()):
        holes.extend((segment, str(n)) for n in range(min(seen), max(seen) + 1) if n not in seen)
    return holes
