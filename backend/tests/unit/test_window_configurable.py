"""ENF-OPS-014 (M1b): the window is a setting; midnight and the changes of hour are handled."""

from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from shelfhold.config import Settings
from shelfhold.jobs.cron import CronSpec
from shelfhold.jobs.window import DEFAULT_WINDOW, MaintenanceWindow


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.req("ENF-OPS-014")
def test_window_configurable_from_m1b() -> None:
    assert Settings().maintenance_window_start == "03:00"
    assert Settings().maintenance_window_end == "07:00"
    assert Settings().timezone == "Europe/Paris"
    custom = Settings(
        maintenance_window_start="01:30", maintenance_window_end="04:00", timezone="UTC"
    )
    window = MaintenanceWindow.from_text(
        custom.maintenance_window_start, custom.maintenance_window_end, custom.timezone
    )
    span = window.bounds_on(date(2026, 11, 4))
    assert (span.start, span.end) == (utc(2026, 11, 4, 1, 30), utc(2026, 11, 4, 4))
    assert MaintenanceWindow.from_text("03:00", "07:00", "Europe/Paris") == DEFAULT_WINDOW


@pytest.mark.req("ENF-OPS-014")
@pytest.mark.parametrize(
    ("start", "end", "zone"),
    [("3h", "07:00", "UTC"), ("03:00", "03:00", "UTC"), ("03:00", "07:00", "Not/AZone"),
     ("25:00", "07:00", "UTC")],
)  # fmt: skip
def test_a_bad_window_is_refused(start: str, end: str, zone: str) -> None:
    with pytest.raises(ValidationError):
        Settings(maintenance_window_start=start, maintenance_window_end=end, timezone=zone)
    with pytest.raises(ValueError, match=r".+"):
        MaintenanceWindow.from_text(start, end, zone)


@pytest.mark.req("ENF-OPS-014")
def test_window_across_midnight() -> None:
    window = MaintenanceWindow.from_text("23:00", "02:00", "Europe/Paris")
    span = window.bounds_on(date(2026, 11, 4))
    assert (span.start, span.end) == (utc(2026, 11, 4, 22), utc(2026, 11, 5, 1))
    # 00:30 local on the 5th belongs to the occurrence that opened on the 4th
    inside = utc(2026, 11, 4, 23, 30)
    assert window.span_containing(inside) == span
    assert window.span_containing(utc(2026, 11, 5, 1)) is None  # the end is excluded
    assert window.span_containing(utc(2026, 11, 4, 12)) is None
    assert window.next_span(inside) == span  # open now: it is the one to finish
    assert window.next_span(utc(2026, 11, 4, 12)) == span
    assert window.clamp(utc(2026, 11, 4, 12)) == span.start
    assert window.clamp(inside) == inside
    assert window.is_occurrence(span)
    assert not window.is_occurrence(DEFAULT_WINDOW.bounds_on(date(2026, 11, 4)))


@pytest.mark.req("ENF-OPS-014", "ENF-TIME-001")
def test_window_across_midnight_on_a_change_of_hour() -> None:
    window = MaintenanceWindow.from_text("23:00", "05:00", "Europe/Paris")
    spring = window.bounds_on(date(2026, 3, 28))  # the night of the 29th: 02:00 -> 03:00
    assert spring.end - spring.start == timedelta(hours=5)
    autumn = window.bounds_on(date(2026, 10, 24))  # the night of the 25th: 03:00 -> 02:00
    assert autumn.end - autumn.start == timedelta(hours=7)
    # whatever the day, an instant lies in at most one occurrence
    day = date(2026, 3, 25)
    for _ in range(10):
        a, b = window.bounds_on(day), window.bounds_on(day + timedelta(days=1))
        assert a.end <= b.start
        day += timedelta(days=1)


@pytest.mark.req("ENF-OPS-014", "ENF-TIME-001")
def test_cron_follows_the_instance_timezone() -> None:
    paris = CronSpec.parse("30 3 * * *").next_after(utc(2026, 11, 4, 12))
    new_york = CronSpec.parse("30 3 * * *").next_after(
        utc(2026, 11, 4, 12), MaintenanceWindow.from_text("03:00", "07:00", "America/New_York").tz
    )
    assert paris == utc(2026, 11, 5, 2, 30)
    assert new_york == utc(2026, 11, 5, 8, 30)
