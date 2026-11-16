"""A position that belongs to no Edition: what a reader carries over when another Edition of the
Work becomes the preferred one (EF-BIB-026, EF-BIB-044).

Two Editions of a Work cut it differently (a volume of eight chapters, then the chapters one by
one), so a position cannot move as it is: a page of one file means nothing in another. What is
kept is neutral: the Work, the part reached (its ordinal and its number), how far in that part,
how far in the whole, whether the Work is finished. It is always `approximate`: it is a guess at
where the reader is, never a proof of what the reader read.

`neutral_of` reads the part reached from the progress per Unit (a page of a volume is already a
chapter and a share of it there, by the bounds of `unit_coverage`: `reading.domain.projection`);
`transpose` finds the part of the other Edition that corresponds: the same number, else the share
of the whole.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

__all__ = [
    "NeutralPosition",
    "SourcePart",
    "neutral_of",
    "transpose",
]

_STARTED: Final = ("in_progress", "read")


def _within(value: float) -> float:
    return min(1.0, max(0.0, value))


@dataclass(frozen=True)
class NeutralPosition:
    work_id: str
    part_ordinal: int  # 1-based rank of the part reached among the parts of the source Edition
    total_progression: float  # 0 to 1, over the whole Work
    completed: bool
    approximate: bool = True
    part_number: float | None = None  # the number of that part: what two Editions share
    part_progression: float | None = None  # 0 to 1, how far in that part, when known

    def __post_init__(self) -> None:
        if not self.approximate:
            raise ValueError("a neutral position is always approximate")
        if not 0.0 <= self.total_progression <= 1.0:
            raise ValueError("totalProgression is between 0 and 1")
        if self.part_progression is not None and not 0.0 <= self.part_progression <= 1.0:
            raise ValueError("part_progression is between 0 and 1")

    def as_json(self) -> dict[str, object]:
        """What is written in the locator of the transposed position."""
        return {
            "work_id": self.work_id,
            "part_ordinal": self.part_ordinal,
            "total_progression": self.total_progression,
            "completed": self.completed,
            "approximate": True,
            "part_number": self.part_number,
            "part_progression": self.part_progression,
        }


@dataclass(frozen=True)
class SourcePart:
    """A part of the source Edition, in reading order: its number, the state the reader's
    progress gives it, and how far in it (`None` when nothing says)."""

    number: float
    state: str  # unread | in_progress | read
    within: float | None


def neutral_of(work_id: str, parts: Sequence[SourcePart]) -> NeutralPosition | None:
    """The furthest part the reader reached (the last one that is started, in reading order),
    or `None` when nothing is started. A part whose share is unknown counts for its start, never
    beyond: the guess is never ahead of what the progress says."""
    reached = [i for i, p in enumerate(parts) if p.state in _STARTED]
    if not reached:
        return None
    index = reached[-1]
    part = parts[index]
    share = 1.0 if part.state == "read" else _within(part.within or 0.0)
    return NeutralPosition(
        work_id=work_id,
        part_ordinal=index + 1,
        total_progression=_within((index + share) / len(parts)),
        completed=index == len(parts) - 1 and part.state == "read",
        part_number=part.number,
        part_progression=None if part.state != "read" and part.within is None else share,
    )


def transpose(position: NeutralPosition, numbers: Sequence[float]) -> tuple[int, float] | None:
    """The part of the other Edition (`numbers`, in reading order) that corresponds: its index and
    how far in it. The same number is the same part; without it, the share of the whole decides.
    A finished Work lands on the last part, whole. `None` for an Edition with no part."""
    if not numbers:
        return None
    if position.completed:
        return len(numbers) - 1, 1.0
    if position.part_number is not None and position.part_number in numbers:
        return numbers.index(position.part_number), _within(position.part_progression or 0.0)
    scaled = position.total_progression * len(numbers)
    index = min(len(numbers) - 1, int(scaled))
    return index, _within(scaled - index)
