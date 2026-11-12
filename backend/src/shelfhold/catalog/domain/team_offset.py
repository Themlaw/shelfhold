"""Numbering offset between teams, checked before any team fallback (EF-BIB-028, GAP-8-05).

Two teams may number the same chapters differently: a team declares a `number_offset`, the shift
between its numbers and the Edition's. Before the election departs from the team that holds the
files (a *fallback*), what the other team offers for the Unit is held against what the team on
disk has: the normalized title and the page count must agree. A gap that no declared offset
explains, or a declared offset that the evidence contradicts, suspends the fallback; the caller
opens a suggestion and says why. A translation is never silently swapped under a number.

Pure: the facts come from the caller. This is the only place the rule lives; the acquisition
side reads its verdict through `catalog.public`.
"""

from dataclasses import dataclass

from shelfhold.catalog.domain.identity import sort_title

__all__ = [
    "OFFSET_INCONSISTENT",
    "OFFSET_UNEXPLAINED",
    "OFFSET_UNVERIFIED",
    "OffsetVerdict",
    "TeamFacts",
    "check_before_fallback",
]

OFFSET_INCONSISTENT = "offset_inconsistent"  # a declared offset, and the evidence contradicts it
OFFSET_UNEXPLAINED = "offset_unexplained"  # no offset known, and the two offers differ
OFFSET_UNVERIFIED = "offset_unverified"  # an offset is declared, and nothing can confirm it


@dataclass(frozen=True)
class TeamFacts:
    """What one team's offer for a Unit says: its declared offset, title and page count."""

    offset: int = 0
    title: str | None = None
    page_count: int | None = None


@dataclass(frozen=True)
class OffsetVerdict:
    ok: bool
    reason: str = ""  # empty when ok


_OK = OffsetVerdict(True)


def _normalized(title: str | None) -> str | None:
    value = sort_title(title) if title else ""
    return value or None  # a blank title says nothing


def check_before_fallback(held: TeamFacts, offered: TeamFacts) -> OffsetVerdict:
    """May the election fall back from the team that holds the files (`held`) to `offered`?

    The evidence is the normalized title and the page count, each compared only when both sides
    give one: what one side does not say proves nothing. A gap is `offset_unexplained` when both
    teams declare the same offset (none, by default) and `offset_inconsistent` when they declare
    different ones. Different offsets with nothing to compare are `offset_unverified`: a declared
    shift is not trusted blindly. Same offsets with nothing to compare leave the fallback alone."""
    titles = (_normalized(held.title), _normalized(offered.title))
    pages = (held.page_count, offered.page_count)
    compared = False
    gap = False
    if titles[0] is not None and titles[1] is not None:
        compared = True
        gap = gap or titles[0] != titles[1]
    if pages[0] is not None and pages[1] is not None:
        compared = True
        gap = gap or pages[0] != pages[1]
    shifted = held.offset != offered.offset
    if gap:
        return OffsetVerdict(False, OFFSET_INCONSISTENT if shifted else OFFSET_UNEXPLAINED)
    if shifted and not compared:
        return OffsetVerdict(False, OFFSET_UNVERIFIED)
    return _OK
