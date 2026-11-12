"""The maintenance window (ENF-OPS-014): 03:00-07:00 by default, in the instance's time zone
(Europe/Paris unless `timezone` says otherwise), instants kept in UTC. From M1b the owner sets it
(`maintenance_window_start`, `maintenance_window_end`); one that ends before it starts crosses
midnight (23:00-02:00): its occurrence is named after the day it opens.

Disks are allowed to sleep outside it: periodic work that touches `/store` or `/library` runs
inside it, in priority order, and stops between two operations when it closes. The default window
lasts four real hours every day of the year: the changes of hour happen at 02:00 or 03:00 local
time on the transition day, never inside 03:00-07:00 once it has started. A window that holds
the change of hour is one hour shorter or longer that night, in real time; a start or an end that
the clocks skip is the instant of the change, so a window wholly in the skipped hour has no
occurrence that night.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shelfhold.infrastructure.clock import Clock

PARIS = ZoneInfo("Europe/Paris")

# Priority of the window jobs, highest first (ENF-OPS-014; passe 5, E9).
WINDOW_JOB_ORDER = (
    "wal_checkpoint",
    "store_verify",
    "hash_compare",
    "integrity_rolling",
    "vacuum_monthly",
    "thumbnail_gc",
)


def window_rank(kind: str) -> int:
    """0 is the first to run; a kind the list does not know goes last."""
    try:
        return WINDOW_JOB_ORDER.index(kind)
    except ValueError:
        return len(WINDOW_JOB_ORDER)


@dataclass(frozen=True)
class Span:
    """One occurrence of the window, `[start, end)` in UTC."""

    start: datetime
    end: datetime

    def contains(self, instant: datetime) -> bool:
        return self.start <= instant < self.end

    @property
    def empty(self) -> bool:
        """A window that sits wholly in the hour the clocks skip (02:00-03:00 on the night of the
        change to summer time) opens and closes at the same instant: that day has no occurrence."""
        return self.end <= self.start


_CLOCK_TEXT = re.compile(r"([01]\d|2[0-3]):([0-5]\d)")


def parse_clock(text: str) -> time:
    """`HH:MM`, 24 hours, nothing else."""
    found = _CLOCK_TEXT.fullmatch(text.strip())
    if found is None:
        raise ValueError(f"{text!r} is not a time of day written HH:MM")
    return time(int(found[1]), int(found[2]))


def parse_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ValueError(f"{name!r} is not a time zone of the IANA database") from None


@dataclass(frozen=True)
class MaintenanceWindow:
    start: time = time(3, 0)
    end: time = time(7, 0)  # at or before `start`: the window crosses midnight
    tz: ZoneInfo = PARIS

    def __post_init__(self) -> None:
        if self.start == self.end:
            raise ValueError("the window must not be empty or last all day")

    @classmethod
    def from_text(cls, start: str, end: str, zone: str) -> "MaintenanceWindow":
        return cls(parse_clock(start), parse_clock(end), parse_zone(zone))

    def _instant(self, day: date, at: time) -> datetime:
        """The UTC instant of a local wall-clock time. In the autumn hour that happens twice, the
        first; a time the clocks skip (02:30 on the night of the change to summer time) is the
        instant of the change itself, so that a window is cut there and never moved (EF-INT-004)."""
        naive = datetime.combine(day, at)
        local = naive.replace(tzinfo=self.tz)
        utc = local.astimezone(UTC)
        if utc.astimezone(self.tz).replace(tzinfo=None) == naive:
            return utc
        before = local.utcoffset() or timedelta(0)  # fold=0: the offset before the change
        after = naive.replace(tzinfo=self.tz, fold=1).utcoffset() or timedelta(0)
        low = (naive - after).replace(tzinfo=UTC)  # the change lies between these two instants
        high = (naive - before).replace(tzinfo=UTC)
        while high - low > timedelta(seconds=1):  # first instant that already reads `after`
            mid = low + (high - low) / 2
            if mid.astimezone(self.tz).utcoffset() == after:
                high = mid
            else:
                low = mid
        return high.replace(microsecond=0)

    def bounds_on(self, day: date) -> Span:
        """The occurrence that opens on local date `day` (it closes the next day if it crosses
        midnight)."""
        close_day = day + timedelta(days=1) if self.end <= self.start else day
        return Span(self._instant(day, self.start), self._instant(close_day, self.end))

    def span_containing(self, instant: datetime) -> Span | None:
        local_day = instant.astimezone(self.tz).date()
        for offset in (0, -1):  # the occurrence of the day before may still be open after midnight
            span = self.bounds_on(local_day + timedelta(days=offset))
            if span.contains(instant):  # (an empty span contains nothing)
                return span
        return None

    def next_span(self, instant: datetime) -> Span:
        """The occurrence that is open at `instant`, else the next one to open."""
        day = instant.astimezone(self.tz).date()
        for offset in range(-1, 3):
            span = self.bounds_on(day + timedelta(days=offset))
            if span.end > instant and not span.empty:
                return span
        raise AssertionError("unreachable: a window opens every day")

    def is_occurrence(self, span: Span) -> bool:
        """`span` is an occurrence of this window (not of the one the owner had before)."""
        return self.bounds_on(span.start.astimezone(self.tz).date()) == span

    def clamp(self, instant: datetime) -> datetime:
        """The earliest instant not before `instant` that lies inside a window."""
        span = self.next_span(instant)
        return max(instant, span.start)


DEFAULT_WINDOW = MaintenanceWindow()


class StoreOrigin(StrEnum):
    PERIODIC = "periodic"  # maintenance, verification, scans: window only
    EVENT = "event"  # a new chapter, an approved request: any time
    USER_REQUEST = "user_request"  # someone asked for it: any time


class OutsideMaintenanceWindowError(PermissionError):
    """Periodic access to `/store` or `/library` outside the maintenance window."""


def ensure_store_access(
    origin: StoreOrigin, *, now: datetime, window: MaintenanceWindow = DEFAULT_WINDOW
) -> None:
    if origin is StoreOrigin.PERIODIC and window.span_containing(now) is None:
        raise OutsideMaintenanceWindowError("periodic access to the store is window-only")


@dataclass(frozen=True)
class WindowRun:
    next_index: int  # the cursor a later run resumes from
    preempted: bool


def run_until(
    steps: Sequence[Callable[[], None]],
    *,
    deadline: datetime,
    clock: Clock,
    start: int = 0,
    stop: Callable[[], bool] | None = None,
) -> WindowRun:
    """Run `steps[start:]`, stopping between two steps once `deadline` has passed.

    A step is never interrupted: it finishes, then the deadline is looked at again.
    """
    index = start
    while index < len(steps):
        if clock.now() >= deadline or (stop is not None and stop()):  # window over, or SIGTERM
            return WindowRun(index, preempted=True)
        steps[index]()
        index += 1
    return WindowRun(index, preempted=False)
