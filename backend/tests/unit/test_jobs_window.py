"""ENF-OPS-014, ENF-TIME-001, EF-INT-003: the fixed window, cron in Europe/Paris, DST."""

from datetime import UTC, date, datetime, timedelta

import pytest

from shelfhold.jobs.cron import CronError, CronSpec
from shelfhold.jobs.window import (
    DEFAULT_WINDOW,
    WINDOW_JOB_ORDER,
    OutsideMaintenanceWindowError,
    StoreOrigin,
    ensure_store_access,
    order_window_jobs,
    run_until,
)
from tests.fakes_clock import ManualClock


def utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


@pytest.mark.req("ENF-OPS-014")
def test_window_is_0300_0700_paris_stored_in_utc() -> None:
    winter = DEFAULT_WINDOW.bounds_on(date(2026, 1, 15))
    summer = DEFAULT_WINDOW.bounds_on(date(2026, 7, 15))
    assert (winter.start, winter.end) == (utc(2026, 1, 15, 2), utc(2026, 1, 15, 6))
    assert (summer.start, summer.end) == (utc(2026, 7, 15, 1), utc(2026, 7, 15, 5))


@pytest.mark.req("ENF-OPS-014", "ENF-TIME-001")
def test_dst_transition_schedule() -> None:
    # The window never straddles a change of hour (02:00 or 03:00 local): it lasts 4 h both days.
    for day in (date(2026, 3, 29), date(2026, 10, 25)):
        span = DEFAULT_WINDOW.bounds_on(day)
        assert span.end - span.start == timedelta(hours=4)
    assert DEFAULT_WINDOW.bounds_on(date(2026, 3, 29)).start == utc(2026, 3, 29, 1)
    assert DEFAULT_WINDOW.bounds_on(date(2026, 10, 25)).start == utc(2026, 10, 25, 2)

    daily = CronSpec.parse("30 3 * * *")
    spring = daily.next_after(utc(2026, 3, 28, 12))
    assert spring == utc(2026, 3, 29, 1, 30)  # 03:30 CEST, 23 h after 03:30 CET
    assert daily.next_after(utc(2026, 3, 28, 2, 30)) == spring
    autumn = daily.next_after(utc(2026, 10, 24, 12))
    assert autumn == utc(2026, 10, 25, 2, 30)  # 03:30 CET, 25 h after 03:30 CEST
    assert daily.next_after(utc(2026, 10, 24, 1, 30)) == autumn

    # 02:30 does not exist on the spring day: it runs once, shifted past the gap.
    skipped = CronSpec.parse("30 2 * * *")
    assert skipped.next_after(utc(2026, 3, 28, 12)) == utc(2026, 3, 29, 1, 30)
    assert skipped.next_after(utc(2026, 3, 29, 1, 30)) == utc(2026, 3, 30, 0, 30)
    # 02:30 happens twice on the autumn day: it runs once, the first time.
    twice = CronSpec.parse("30 2 * * *")
    first = twice.next_after(utc(2026, 10, 24, 12))
    assert first == utc(2026, 10, 25, 0, 30)
    assert twice.next_after(first) == utc(2026, 10, 26, 1, 30)


@pytest.mark.req("EF-INT-003")
def test_cron_fields_and_errors() -> None:
    spec = CronSpec.parse("*/15 8-9 * * 1,5")  # Monday and Friday, 08:00-09:45 Paris
    assert spec.next_after(utc(2026, 11, 4, 12)) == utc(2026, 11, 6, 7, 0)  # Friday 08:00 CET
    for bad in ("", "* * * *", "61 * * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *"):
        with pytest.raises(CronError):
            CronSpec.parse(bad)


@pytest.mark.req("ENF-OPS-014")
def test_clamp_moves_an_instant_into_the_window() -> None:
    inside = utc(2026, 11, 4, 4)
    assert DEFAULT_WINDOW.clamp(inside) == inside
    assert DEFAULT_WINDOW.clamp(utc(2026, 11, 4, 12)) == utc(2026, 11, 5, 2)
    assert DEFAULT_WINDOW.clamp(utc(2026, 11, 4, 0)) == utc(2026, 11, 4, 2)
    assert DEFAULT_WINDOW.span_containing(utc(2026, 11, 4, 6)) is None  # the end is exclusive


@pytest.mark.req("ENF-OPS-014")
def test_window_jobs_preempted_at_window_end() -> None:
    clock = ManualClock(utc(2026, 11, 4, 5, 58))  # 06:58 Paris, two minutes before the end
    deadline = utc(2026, 11, 4, 6)
    done: list[int] = []

    def step(n: int):
        def run() -> None:
            done.append(n)
            clock.advance(seconds=50)  # an operation is never cut in the middle

        return run

    steps = [step(n) for n in range(5)]
    report = run_until(steps, deadline=deadline, clock=clock)
    assert done == [0, 1, 2]  # the third started at 05:59:40 and finished after the end
    assert report.preempted is True
    assert report.next_index == 3  # the cursor the next night resumes from
    clock.set(utc(2026, 11, 5, 2))
    resumed = run_until(steps, deadline=utc(2026, 11, 5, 6), clock=clock, start=report.next_index)
    assert done == [0, 1, 2, 3, 4]
    assert resumed.preempted is False
    assert resumed.next_index == 5


@pytest.mark.req("ENF-OPS-014")
def test_window_jobs_priority_order() -> None:
    assert WINDOW_JOB_ORDER == (
        "wal_checkpoint",
        "store_verify",
        "hash_compare",
        "integrity_rolling",
        "vacuum_monthly",
        "thumbnail_gc",
    )
    shuffled = ["thumbnail_gc", "wal_checkpoint", "integrity_rolling", "store_verify"]
    assert order_window_jobs(shuffled) == [
        "wal_checkpoint",
        "store_verify",
        "integrity_rolling",
        "thumbnail_gc",
    ]


@pytest.mark.req("ENF-OPS-014")
def test_no_store_access_outside_window_unless_user_request() -> None:
    noon = utc(2026, 11, 4, 11)
    with pytest.raises(OutsideMaintenanceWindowError):
        ensure_store_access(StoreOrigin.PERIODIC, now=noon)
    ensure_store_access(StoreOrigin.USER_REQUEST, now=noon)
    ensure_store_access(StoreOrigin.PERIODIC, now=utc(2026, 11, 4, 3))


@pytest.mark.req("ENF-OPS-014")
def test_event_driven_export_allowed_outside_window() -> None:
    ensure_store_access(StoreOrigin.EVENT, now=utc(2026, 11, 4, 11))
