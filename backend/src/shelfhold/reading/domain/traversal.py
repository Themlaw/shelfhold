"""When a reader has really read a Unit: a plausible pass over it (ADR-0053, MAJ-2, amended).

A reader's report of where it is says nothing about the pages it did not turn: a jump to the end
of a volume (table of contents, a look at the last page) is a position, not a reading. A Unit is
marked **read** when the reader has plausibly gone through all of it.

**The rule, by speed.** KOReader's sync plugin does not push after every page: `auto_sync` is off
and `pages_before_update` is `nil` by default (`plugins/kosync.koplugin/main.lua`), so a position
arrives when the book is closed or the device sleeps, a chapter apart. The old rule (at most 3
pages between two positions) would then mark nothing. A forward move from the position A to the
position B is **plausible** when:

* it is at most `SMALL_GAP` pages (the old rule, always admitted), or
* `(pages advanced) / (server_ts(B) - server_ts(A)) <= max_pages_per_minute` (default 20,
  `reading_max_pages_per_minute`): wide enough for a manga or a webtoon, and a look at 200 pages
  in 30 seconds is 400 a minute.

The plausible moves cover intervals of pages. A Unit is read when one merged interval covers it
whole (from its first pages, `HEAD` pages of slack, to its last page). Only `server_ts` counts for
the speed: a `client_ts` is the device's word and never enters it. A move with no earlier position
to start from, a backward move, one that is too fast, covers nothing. The anchor A of a position
is the last known position of the profile on the same content; when there is none, the delivery of
the file to the profile (page 0), if one was recorded.

Two modes, by what the positions carry:

* **pages**: the Units have known page bounds and the positions have integer pages;
* **share**: a file that holds one Unit and positions with a percent; a share becomes pages with
  the page count of the content when it is known (without it, only the small gap, 5 %, applies).
  A file of several Units with no page bounds cannot say where a Unit begins: nothing is read.

A mark is journalled by the caller and can be undone; `after` keeps an undone mark undone: only
moves that end later than the undo count again.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import pairwise
from typing import Final

PAGES: Final = "pages"
SHARE: Final = "share"
DEFAULT_MAX_PAGES_PER_MINUTE: Final = 20.0
SMALL_GAP: Final = {PAGES: 3.0, SHARE: 0.05}
HEAD: Final = {PAGES: 2.0, SHARE: 0.05}  # the first 3 pages are start .. start + 2
_ADJACENT: Final = {PAGES: 1.0, SHARE: 1e-9}
_EPS: Final = 1e-9


@dataclass(frozen=True)
class Step:
    """One position: where (a page, or a share of the file) and when the **server** got it."""

    x: float
    ts: datetime


@dataclass(frozen=True)
class Span:
    unit_id: str
    start: float
    end: float


def plausible_moves(
    steps: Sequence[Step],
    *,
    mode: str,
    max_pages_per_minute: float = DEFAULT_MAX_PAGES_PER_MINUTE,
    pages_per_unit_of_x: float | None = None,
) -> list[tuple[float, float, datetime]]:
    """The forward moves between successive positions that are plausible, as
    `(from, to, when it ended)`. `pages_per_unit_of_x` turns a share into pages (the page count)."""
    moves: list[tuple[float, float, datetime]] = []
    for a, b in pairwise(steps):
        advance = b.x - a.x
        if advance <= _EPS:
            continue
        if advance <= SMALL_GAP[mode] + _EPS:
            moves.append((a.x, b.x, b.ts))
            continue
        pages = advance if mode == PAGES else None
        if pages is None and pages_per_unit_of_x is not None:
            pages = advance * pages_per_unit_of_x
        minutes = (b.ts - a.ts).total_seconds() / 60
        if pages is not None and minutes > 0 and pages / minutes <= max_pages_per_minute + _EPS:
            moves.append((a.x, b.x, b.ts))
    return moves


def traversed_units(
    steps: Sequence[Step],
    spans: Sequence[Span],
    *,
    mode: str,
    max_pages_per_minute: float = DEFAULT_MAX_PAGES_PER_MINUTE,
    pages_per_unit_of_x: float | None = None,
    after: Mapping[str, datetime] | None = None,
) -> list[str]:
    """The Units (ids, in file order) the plausible moves of `steps` (oldest server date first)
    cover whole."""
    moves = plausible_moves(
        steps,
        mode=mode,
        max_pages_per_minute=max_pages_per_minute,
        pages_per_unit_of_x=pages_per_unit_of_x,
    )
    found: list[str] = []
    for span in spans:
        since = (after or {}).get(span.unit_id)
        usable = sorted((lo, hi) for lo, hi, ts in moves if since is None or ts > since)
        merged: list[list[float]] = []
        for lo, hi in usable:
            if merged and lo <= merged[-1][1] + _ADJACENT[mode]:
                merged[-1][1] = max(merged[-1][1], hi)
            else:
                merged.append([lo, hi])
        if any(lo <= span.start + HEAD[mode] + _EPS and hi >= span.end - _EPS for lo, hi in merged):
            found.append(span.unit_id)
    return found
