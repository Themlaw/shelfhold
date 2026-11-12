"""The windows as an operator reads them (EF-INT-004): stored and compared in UTC, rendered in the
instance's local time, with the real length of the occurrence.

`jobs.window` decides; this module only describes. An occurrence is `[start, end)` in UTC; its
local rendering carries the offset in force at each end, so that the night the clocks change reads
as it is: a 01:00-06:00 window lasts four real hours on the night of the change to summer time and
six on the night of the change back, and `duration_s` says so. A window that sits wholly in the
skipped hour has no occurrence that day (`window.Span.empty`) and is not listed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from shelfhold.jobs.window import MaintenanceWindow, Span

__all__ = ["SpanView", "WindowView", "describe", "upcoming"]


@dataclass(frozen=True)
class SpanView:
    start_utc: str
    end_utc: str
    start_local: str  # ISO 8601 with the offset in force
    end_local: str
    duration_s: int  # real seconds, not the difference of two clock readings
    open: bool  # open at the instant given to `describe`


@dataclass(frozen=True)
class WindowView:
    timezone: str
    start: str  # the configured local times, `HH:MM`
    end: str
    crosses_midnight: bool
    current: SpanView | None  # the occurrence open now
    next: SpanView  # the next one to open


def _utc(instant: datetime) -> str:
    return instant.strftime("%Y-%m-%dT%H:%M:%SZ")


def _view(span: Span, window: MaintenanceWindow, now: datetime) -> SpanView:
    return SpanView(
        start_utc=_utc(span.start),
        end_utc=_utc(span.end),
        start_local=span.start.astimezone(window.tz).isoformat(),
        end_local=span.end.astimezone(window.tz).isoformat(),
        duration_s=int((span.end - span.start).total_seconds()),
        open=span.contains(now),
    )


def describe(window: MaintenanceWindow, now: datetime) -> WindowView:
    current = window.span_containing(now)
    following = window.next_span(current.end if current is not None else now)
    return WindowView(
        timezone=str(window.tz),
        start=window.start.strftime("%H:%M"),
        end=window.end.strftime("%H:%M"),
        crosses_midnight=window.end <= window.start,
        current=_view(current, window, now) if current is not None else None,
        next=_view(following, window, now),
    )


def upcoming(window: MaintenanceWindow, now: datetime, *, count: int) -> list[SpanView]:
    """The next `count` occurrences, the open one first."""
    found: list[SpanView] = []
    cursor = now
    while len(found) < count:
        span = window.next_span(cursor)
        found.append(_view(span, window, now))
        cursor = span.end + timedelta(seconds=1)
    return found
