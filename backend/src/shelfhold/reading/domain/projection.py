"""A position in a file, projected on one Unit the file covers (EF-LEC-014, EF-BIB-026).

The state of a Unit is read from where the reader is in the file that holds it:

* with known bounds (`start_page`, `end_page` in the coverage) and an integer page in the
  position, it is exact: under way from the first page, reached once the last page is;
* otherwise it is proportional to the Unit's rank among the Units of the file, and `approximate`,
  except for a file that holds a single Unit (its percent is then the Unit's own);
* a position taken on a **replaced** content (another hash than the file now carries) never uses
  its page, which was counted in a file that is no longer there: it is proportional and
  `approximate` (invariant I30). Which Units it reaches is the caller's: the ones the file covered
  when the position was written.

`trusted` tells who said it. A `server` position (the read state Kavita keeps) is taken as it is:
reaching the end of a Unit is `read`. A `device` position is a report of where the reader is, and a
look at the last page is not a reading: it never goes beyond `in_progress`; `read` comes from a
page-by-page traversal (`reading.domain.traversal`, ADR-0053).

`within` is how far the position got **in the Unit** (0 to 1), the only thing two positions on
different files can be compared by: 60 % of a volume of two chapters is 20 % of the second.

This is the rule the SQL view `reading_progress` implements; `test_reading_progress.py` proves
the two equal.
"""

from dataclasses import dataclass
from typing import Final, Literal

State = Literal["unread", "in_progress", "read"]
STATE_RANK: Final[dict[str, int]] = {"unread": 0, "in_progress": 1, "read": 2}


@dataclass(frozen=True)
class Bounds:
    """Where a Unit sits in its file: its first and last page when known, its rank (from 0)
    among the `count` Units the file covers."""

    start_page: int | None
    end_page: int | None
    rank0: int
    count: int


@dataclass(frozen=True)
class Projected:
    state: State
    approximate: bool
    within: float | None = None  # how far in the Unit, 0 to 1; None when nothing says


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))


def project(
    *,
    page: int | None,
    percent: float | None,
    bounds: Bounds,
    replaced: bool = False,
    trusted: bool = True,
) -> Projected:
    first, last = bounds.start_page, bounds.end_page
    if not replaced and page is not None and first is not None and last is not None:
        done, started = page >= last, page >= first
        approximate = False
        if last > first:
            within: float | None = _clamp((page - first) / (last - first))
        else:
            within = 1.0 if page >= last else 0.0
    else:
        share = 0.0 if percent is None else percent
        done = share >= (bounds.rank0 + 1) / bounds.count
        started = share > bounds.rank0 / bounds.count
        approximate = bounds.count > 1 or percent is None
        within = None if percent is None else _clamp(percent * bounds.count - bounds.rank0)
    if replaced:
        approximate = True
    if done and trusted:
        state: State = "read"
    elif done or started:
        state = "in_progress"
    else:
        state = "unread"
    return Projected(state, approximate, within)
