"""How the positions of one reader on one Unit become one answer (EF-LEC-014, EF-LEC-007).

**One rule: the furthest position.** Among all the positions that reach a Unit, on any file that
covers it, the one that got the furthest wins:

1. the highest state (`read` > `in_progress` > `unread`): a Unit that was read never goes back to
   unread because a reader reopened the book at the start, restored an old backup or sent late
   what it had read before;
2. then an exact position before an approximate one (a position on the file as it is now beats one
   on a replaced content, or a proportion);
3. then how far it got **in the Unit** (`Projected.within`), never the percent of the file: the
   percents of two files are not the same quantity;
4. then the latest date, `client_ts` **bounded by `server_ts`** (a clock set in the future counts
   as the date the server received the position);
5. then the highest position id, so that the answer is total.

`furthest` gives `last_position_id`, the position to hand to a reader that asks where the furthest
point is (the kosync answer, ADR-0053 section 5). `latest` gives `latest_position_id`: the last
position written, which is where the reader is now, even if it went back (resume, "continue
reading"). A relay that needs the furthest point reads the first; one that needs the current point
reads the second.

The rule never marks a Unit that no position reaches: a relay that records a position on the
chapter it falls in marks nothing in the chapters it skipped (ADR-0053).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final

from shelfhold.reading.domain.projection import STATE_RANK, Projected


def bounded_ts(client_ts: datetime, server_ts: datetime) -> datetime:
    """The date a position counts for: the client's, never later than the server's."""
    return min(client_ts, server_ts)


@dataclass(frozen=True)
class Observation:
    """What one position says about one Unit."""

    position_id: str
    projected: Projected
    client_ts: datetime
    server_ts: datetime


def furthest(observations: Sequence[Observation]) -> Observation:
    if not observations:
        raise ValueError("no position to merge")
    return max(
        observations,
        key=lambda o: (
            STATE_RANK[o.projected.state],
            not o.projected.approximate,
            -1.0 if o.projected.within is None else o.projected.within,
            bounded_ts(o.client_ts, o.server_ts),
            o.position_id,
        ),
    )


def latest(observations: Sequence[Observation]) -> Observation:
    if not observations:
        raise ValueError("no position to merge")
    return max(observations, key=lambda o: (bounded_ts(o.client_ts, o.server_ts), o.position_id))


# D8 (ADR-0055, Q47): a conflict is proposed only if the position that lost is further than the one
# that won by more than 2 % of progression, and, for a support of pages, by at least 3 pages (in
# reflow the number of pages depends on the window: `totalProgression` alone decides).
CONFLICT_PROGRESSION: Final = 0.02
CONFLICT_MIN_PAGES: Final = 3
_EPSILON: Final = 1e-9  # 0.32 - 0.30 is 0.02 and a bit, in floating point: that is not above


@dataclass(frozen=True)
class Reported:
    """One position of the integrated reader, as the merge sees it."""

    position_id: str
    device_id: str | None
    progression: float
    client_ts: datetime
    server_ts: datetime
    content_hash: str
    index: int | None = None  # the page, for a support of pages
    count: int | None = None


def winner(reports: Sequence[Reported]) -> Reported:
    """Where the reader is: the latest position, `client_ts` bounded by `server_ts` (EF-LEC-007).
    It may be behind the furthest one; going back is allowed, and the history keeps the rest."""
    if not reports:
        raise ValueError("no position to merge")
    return max(reports, key=lambda r: (bounded_ts(r.client_ts, r.server_ts), r.position_id))


def conflicting(won: Reported, others: Sequence[Reported]) -> Reported | None:
    """The position, among `others`, that the reader should be asked about, or None.

    Only what another device said counts (going back on one's own device is not a divergence), on
    the same content (the progress of two files is not the same quantity), and only if it got
    further than `won` by more than D8's threshold. Of several, the furthest."""
    best: Reported | None = None
    for other in others:
        if other.device_id == won.device_id or other.content_hash != won.content_hash:
            continue
        if other.progression - won.progression - CONFLICT_PROGRESSION <= _EPSILON:
            continue
        if (
            other.index is not None
            and won.index is not None
            and other.index - won.index < (CONFLICT_MIN_PAGES)
        ):
            continue
        if best is None or (other.progression, other.position_id) > (
            best.progression,
            best.position_id,
        ):
            best = other
    return best
