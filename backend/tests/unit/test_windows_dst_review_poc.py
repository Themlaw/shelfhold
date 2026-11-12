"""Review PoCs of PR #201 (M3 L13, EF-INT-004): the skipped hour of the change to summer time.

The module says a window keeps its wall-clock meaning ("one hour shorter or longer that night, in
real time") and that a window wholly in the skipped hour has no occurrence. A start or an end that
falls in the skipped hour is read with `fold=0`, i.e. one hour later in clock time, so the window
moves instead of being cut: it is open at an instant outside its configured hours, and closed at
an instant inside them. Red until the bounds are clamped to the transition instant.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from shelfhold.jobs.window import MaintenanceWindow

SPRING_2027 = date(2027, 3, 28)  # Europe/Paris: 02:00 CET -> 03:00 CEST (01:00Z)

pytestmark = pytest.mark.req("EF-INT-004")


def utc(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def window(start: str, end: str) -> MaintenanceWindow:
    return MaintenanceWindow.from_text(start, end, "Europe/Paris")


def test_review_poc_a_window_in_the_skipped_hour_is_open_at_03_15_cest() -> None:
    """02:00-02:30 lies wholly in the skipped hour: the docstring of `Span.empty` says that day has
    no occurrence. It gets one, 03:00-03:30 CEST, outside the hours the owner chose."""
    gap = window("02:00", "02:30")
    assert gap.span_containing(utc(2027, 3, 28, 1, 15)) is None  # 03:15 CEST


def test_review_poc_a_window_half_in_the_skipped_hour_loses_its_real_half() -> None:
    """02:30-03:30: on the night of the change, 03:00-03:30 CEST does exist and is inside the
    window by any reading. The occurrence collapses to an empty span and nothing runs."""
    half = window("02:30", "03:30")
    assert half.span_containing(utc(2027, 3, 28, 1, 15)) is not None  # 03:15 CEST
    assert half.next_span(utc(2027, 3, 28, 0, 0)).start.date() == SPRING_2027


def test_review_poc_a_window_across_midnight_overruns_its_end_by_an_hour() -> None:
    """22:00-02:30 on the Saturday before the change: the window must be closed at 03:15 CEST
    (after its 02:30 end in any clock); `fold=0` reads 02:30 as 03:30 CEST and keeps it open, while
    01:00-06:00 the same night is cut to 4 h. One rule for the start, another for the end."""
    late = window("22:00", "02:30")
    assert late.span_containing(utc(2027, 3, 28, 1, 15)) is None  # 03:15 CEST
