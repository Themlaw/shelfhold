"""M3 L13, EF-INT-004: windows are kept in UTC, rendered in local time, and stay coherent on the
nights the clocks change, in both directions, for a window that crosses midnight, and for one that
sits wholly in the skipped hour."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta

import pytest

from shelfhold.jobs.window import MaintenanceWindow
from shelfhold.jobs.windows import describe, upcoming

SPRING = date(2026, 3, 29)  # Europe/Paris: 02:00 CET -> 03:00 CEST (the night is 1 h shorter)
AUTUMN = date(2026, 10, 25)  # 03:00 CEST -> 02:00 CET (the night is 1 h longer)
HOUR = 3600


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


def window(start: str, end: str, zone: str = "Europe/Paris") -> MaintenanceWindow:
    return MaintenanceWindow.from_text(start, end, zone)


@pytest.mark.req("EF-INT-004")
def test_time_windows_utc_rendered_local_dst() -> None:
    one_to_six = window("01:00", "06:00")
    ordinary = one_to_six.bounds_on(date(2026, 3, 10))
    spring = one_to_six.bounds_on(SPRING)
    autumn = one_to_six.bounds_on(AUTUMN)
    assert (ordinary.end - ordinary.start).total_seconds() == 5 * HOUR
    assert (spring.end - spring.start).total_seconds() == 4 * HOUR  # the window is one hour short
    assert (autumn.end - autumn.start).total_seconds() == 6 * HOUR  # and one hour long
    # stored in UTC: 01:00 CET is 00:00Z, and 06:00 CEST is 04:00Z
    assert (spring.start, spring.end) == (utc(2026, 3, 29, 0), utc(2026, 3, 29, 4))
    assert (autumn.start, autumn.end) == (utc(2026, 10, 24, 23), utc(2026, 10, 25, 5))
    # rendered local, with the offset in force at each end
    shown = describe(one_to_six, utc(2026, 3, 28, 12)).next
    assert shown.start_local == "2026-03-29T01:00:00+01:00"
    assert shown.end_local == "2026-03-29T06:00:00+02:00"
    assert shown.duration_s == 4 * HOUR
    shown = describe(one_to_six, utc(2026, 10, 24, 12)).next
    assert shown.start_local == "2026-10-25T01:00:00+02:00"
    assert shown.end_local == "2026-10-25T06:00:00+01:00"
    assert shown.duration_s == 6 * HOUR


@pytest.mark.req("EF-INT-004")
@pytest.mark.parametrize("day", [SPRING, AUTUMN])
def test_default_window_is_four_real_hours_on_the_change_days(day: date) -> None:
    """The change of hour is at 02:00 or 03:00 local, outside 03:00-07:00 (ENF-OPS-014)."""
    span = window("03:00", "07:00").bounds_on(day)
    assert (span.end - span.start).total_seconds() == 4 * HOUR


@pytest.mark.req("EF-INT-004")
def test_a_window_across_midnight_over_the_change_of_hour() -> None:
    late = window("23:00", "02:30")
    # Saturday night into the Sunday of the change to summer time: 02:30 does not exist, so the
    # window is cut at the change itself (02:00 CET = 01:00Z), never moved to 03:30 CEST
    spring = late.bounds_on(date(2026, 3, 28))
    assert spring.start == utc(2026, 3, 28, 22)
    assert spring.end == utc(2026, 3, 29, 1)
    assert spring.contains(utc(2026, 3, 29, 0, 59))
    assert not spring.contains(utc(2026, 3, 29, 1))
    # into the Sunday of the change back: 02:30 happens twice; the first one closes the window
    autumn = late.bounds_on(date(2026, 10, 24))
    assert autumn.start == utc(2026, 10, 24, 21)
    assert autumn.end == utc(2026, 10, 25, 0, 30)
    # an instant after midnight belongs to the occurrence that opened the evening before
    assert late.span_containing(utc(2026, 10, 25, 0, 10)) == autumn
    assert late.span_containing(utc(2026, 10, 25, 0, 30)) is None
    # and the next occurrence is the one of the following evening, a normal one
    follower = late.next_span(utc(2026, 10, 25, 0, 30))
    assert follower == late.bounds_on(date(2026, 10, 25))
    assert (follower.end - follower.start).total_seconds() == 3.5 * HOUR


@pytest.mark.req("EF-INT-004")
def test_a_window_inside_the_skipped_hour_has_no_occurrence_that_day() -> None:
    gap = window("02:00", "03:00")
    skipped = gap.bounds_on(SPRING)
    assert skipped.empty  # opens and closes at the same instant: it never held anything
    for minute in range(0, 24 * 60, 15):
        instant = utc(2026, 3, 29, 0) + timedelta(minutes=minute)
        assert gap.span_containing(instant) is None
    # the next window to open is the one of Monday, a normal hour
    following = gap.next_span(utc(2026, 3, 28, 23))
    assert not following.empty
    assert following.start == utc(2026, 3, 30, 0)  # 02:00 CEST on Monday
    assert (following.end - following.start).total_seconds() == HOUR


@pytest.mark.req("EF-INT-004")
def test_a_window_inside_the_repeated_hour_lasts_two_real_hours() -> None:
    repeated = window("02:00", "03:00").bounds_on(AUTUMN)
    assert (repeated.end - repeated.start).total_seconds() == 2 * HOUR


@pytest.mark.req("EF-INT-004")
@pytest.mark.parametrize(
    "bounds", [("03:00", "07:00"), ("01:00", "06:00"), ("23:00", "02:00"), ("02:00", "03:00")]
)
@pytest.mark.parametrize("zone", ["Europe/Paris", "America/New_York", "UTC"])
def test_every_instant_of_the_change_days_is_in_at_most_one_occurrence(
    bounds: tuple[str, str], zone: str
) -> None:
    """`span_containing`, `next_span` and `is_occurrence` agree on every quarter hour around the
    nights the clocks change, in either direction, whatever the zone."""
    w = window(*bounds, zone)
    start = utc(2026, 3, 27)
    for step in range(0, 4 * 24 * 4):  # four days from the 27th, then the same in October
        for base in (start, utc(2026, 10, 23)):
            instant = base + timedelta(minutes=15 * step)
            containing = [
                span
                for d in range(-2, 3)
                if (
                    span := w.bounds_on(instant.astimezone(w.tz).date() + timedelta(days=d))
                ).contains(instant)
            ]
            assert len(containing) <= 1
            found = w.span_containing(instant)
            assert found == (containing[0] if containing else None)
            following = w.next_span(instant)
            assert following.end > instant
            assert not following.empty
            if found is not None:
                assert following == found
            assert w.is_occurrence(following)


@pytest.mark.req("EF-INT-004")
def test_the_upcoming_windows_are_listed_in_order_across_the_change() -> None:
    listed = upcoming(window("01:00", "06:00"), utc(2026, 3, 27, 12), count=4)
    assert [v.start_local[:10] for v in listed] == [
        "2026-03-28", "2026-03-29", "2026-03-30", "2026-03-31",
    ]  # fmt: skip
    assert [v.duration_s for v in listed] == [5 * HOUR, 4 * HOUR, 5 * HOUR, 5 * HOUR]
    assert listed[0].open is False


@pytest.mark.req("EF-INT-004")
def test_describe_says_which_occurrence_is_open_now() -> None:
    w = window("03:00", "07:00")
    inside = describe(w, utc(2026, 11, 12, 3))  # 04:00 in Paris
    assert inside.current is not None and inside.current.open
    assert inside.next.start_utc == "2026-11-13T02:00:00Z"
    outside = describe(w, utc(2026, 11, 12, 12))
    assert outside.current is None
    assert outside.next.start_utc == "2026-11-13T02:00:00Z"
    assert (outside.start, outside.end, outside.crosses_midnight) == ("03:00", "07:00", False)
    assert describe(window("23:00", "02:00"), utc(2026, 11, 12, 12)).crosses_midnight is True
    assert time(3, 0) == w.start
