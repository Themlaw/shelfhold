"""When a reader has really read a Unit: a page-by-page traversal (ADR-0053, MAJ-2).

A reader's report of where it is says nothing about the pages it did not turn: a jump to the end
of a volume (table of contents, a look at the last page) is a position, not a reading. A Unit is
marked **read** only when a chain of the reader's positions, from the same device on the same
content, goes from its first pages to its last page and then into the next Unit (or ends on the
last page, for the last Unit of the file), with no gap above `gap` between two successive
positions. The chain may cross several Units, and a gap breaks it.

Two modes, by what the positions carry:

* **pages**: the file's Units have known page bounds and the positions have integer pages. At most
  3 pages between two positions; the first pages of a Unit are its first 3. The Units are
  `Span(start, end)` in pages.
* **share**: a file that holds one Unit and positions with a percent. At most 5 % between two
  positions, the first 5 % at the start. (A file of several Units with no page bounds cannot say
  where a Unit begins: nothing is marked read there.)

The values are defaults (ADR-0053: "valeurs par défaut proposées"). A mark is journalled by the
caller and can be undone; `after` keeps an undone mark undone: only positions later than the undo
count again.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

PAGES: Final = "pages"
SHARE: Final = "share"
_GAP: Final = {PAGES: 3.0, SHARE: 0.05}
_HEAD: Final = {PAGES: 2.0, SHARE: 0.05}  # the first 3 pages are start .. start + 2
_EPS: Final = 1e-9


@dataclass(frozen=True)
class Step:
    """One position of the chain: where (a page, or a share of the file), and when."""

    x: float
    ts: datetime


@dataclass(frozen=True)
class Span:
    unit_id: str
    start: float
    end: float


def _runs(steps: Sequence[Step], gap: float) -> list[list[Step]]:
    runs: list[list[Step]] = []
    for step in steps:
        if runs and abs(step.x - runs[-1][-1].x) <= gap + _EPS:
            runs[-1].append(step)
        else:
            runs.append([step])
    return runs


def traversed_units(
    steps: Sequence[Step],
    spans: Sequence[Span],
    *,
    mode: str,
    after: Mapping[str, datetime] | None = None,
) -> list[str]:
    """The Units (ids, in file order) the chain of `steps` (oldest first) has traversed."""
    gap, head = _GAP[mode], _HEAD[mode]
    found: list[str] = []
    for index, span in enumerate(spans):
        since = (after or {}).get(span.unit_id)
        usable = [s for s in steps if since is None or s.ts > since]
        following = spans[index + 1] if index + 1 < len(spans) else None
        for run in _runs(usable, gap):
            start = next(
                (
                    i
                    for i, s in enumerate(run)
                    if span.start - _EPS <= s.x <= span.start + head + _EPS
                ),
                None,
            )
            if start is None:
                continue
            end = next((i for i in range(start, len(run)) if run[i].x >= span.end - _EPS), None)
            if end is None:
                continue
            if following is None or any(s.x >= following.start - _EPS for s in run[end + 1 :]):
                found.append(span.unit_id)
                break
    return found
