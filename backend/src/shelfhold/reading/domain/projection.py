"""A position in a file, projected on one Unit the file covers (EF-LEC-014, EF-BIB-026).

The state of a Unit is read from where the reader is in the file that holds it:

* with known bounds (`start_page`, `end_page` in the coverage) and a page in the position, it is
  exact: under way from the first page, read once the last page is reached;
* otherwise it is proportional to the Unit's rank among the Units of the file, and `approximate`,
  except for a file that holds a single Unit (its percent is then the Unit's own);
* a position taken on a **replaced** content (another hash than the file now carries) never uses
  its page, which was counted in a file that is no longer there: it is proportional and
  `approximate` (invariant I30).

This is the rule the SQL view `reading_progress` implements; `test_reading_progress.py` proves
the two equal. The rule is the same for every reader of the view (Kavita, kosync, the
integrated reader).
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


def project(
    *, page: int | None, percent: float | None, bounds: Bounds, replaced: bool = False
) -> Projected:
    first, last = bounds.start_page, bounds.end_page
    if not replaced and page is not None and first is not None and last is not None:
        done, started = page >= last, page >= first
        approximate = False
    else:
        share = 0.0 if percent is None else percent
        done = share >= (bounds.rank0 + 1) / bounds.count
        started = share > bounds.rank0 / bounds.count
        approximate = bounds.count > 1 or percent is None
    if replaced:
        approximate = True
    return Projected("read" if done else "in_progress" if started else "unread", approximate)
