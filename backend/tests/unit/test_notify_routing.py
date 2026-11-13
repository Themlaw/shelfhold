"""EF-INT-021: when a notification leaves, decided by pure rules (no database, no clock)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from shelfhold.integrations.notify.routing import (
    DEFAULT_QUIET,
    DIGEST_HOUR,
    Recipient,
    classify,
    delivery_time,
    effective_quiet,
    hold_until,
    next_digest_at,
)


def person(**kw: object) -> Recipient:
    base: dict[str, object] = {
        "user_id": "u", "role": "member", "timezone": "UTC", "notify_mode": "digest",
        "quiet_start": None, "quiet_end": None,
    }  # fmt: skip
    return Recipient(**{**base, **kw})  # type: ignore[arg-type]


def utc(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, tzinfo=UTC)


@pytest.mark.req("EF-INT-021")
def test_default_quiet_hours_are_21_to_07_when_the_profile_set_none() -> None:
    assert DEFAULT_QUIET == ("21:00", "07:00")
    assert effective_quiet(person()) == DEFAULT_QUIET
    assert effective_quiet(person(quiet_start="23:00", quiet_end="06:30")) == ("23:00", "06:30")


@pytest.mark.req("EF-INT-021")
def test_classify_only_request_arrival_and_outage_can_be_immediate() -> None:
    assert classify("ops.request_arrived") == "request_arrived"
    assert classify("unit.stored") == "chapters"
    assert classify("target.unreachable") == "outage"
    assert classify("source.broken") == "outage"
    for other in ("disk.low", "backup.failed", "series.gap", "job.dead_lettered", "nothing"):
        assert classify(other) is None  # not a per-person notification at all


@pytest.mark.req("EF-INT-021")
def test_digest_is_at_8_local_time_of_the_profile() -> None:
    assert DIGEST_HOUR == 8
    assert next_digest_at(person(), utc(5, 9)) == utc(6, 8)  # today's 8:00 has passed
    assert next_digest_at(person(), utc(5, 7, 59)) == utc(5, 8)
    paris = person(timezone="Europe/Paris")  # UTC+1 in November
    assert next_digest_at(paris, utc(5, 9)) == utc(6, 7)
    assert next_digest_at(paris, utc(5, 6)) == utc(5, 7)
    tokyo = person(timezone="Asia/Tokyo")  # UTC+9
    assert next_digest_at(tokyo, utc(5, 9)) == utc(5, 23)


@pytest.mark.req("EF-INT-021")
def test_digest_hour_follows_the_summer_time_change() -> None:
    paris = person(timezone="Europe/Paris")
    summer = datetime(2026, 7, 1, 12, tzinfo=UTC)  # UTC+2: 8:00 local is 6:00 UTC
    assert next_digest_at(paris, summer) == datetime(2026, 7, 2, 6, tzinfo=UTC)
    winter = datetime(2026, 3, 27, 12, tzinfo=UTC)  # UTC+1 still: 8:00 local is 7:00 UTC
    assert next_digest_at(paris, winter) == datetime(2026, 3, 28, 7, tzinfo=UTC)
    # clocks go forward at 02:00 local on the 29th: that morning's 8:00 is already UTC+2
    night = datetime(2026, 3, 28, 12, tzinfo=UTC)
    assert next_digest_at(paris, night) == datetime(2026, 3, 29, 6, tzinfo=UTC)
    after = datetime(2026, 3, 29, 12, tzinfo=UTC)
    assert next_digest_at(paris, after) == datetime(2026, 3, 30, 6, tzinfo=UTC)


@pytest.mark.req("EF-INT-021")
def test_a_wrong_timezone_falls_back_to_utc_never_raises() -> None:
    assert next_digest_at(person(timezone="Not/AZone"), utc(5, 9)) == utc(6, 8)


@pytest.mark.req("EF-INT-021")
def test_hold_until_is_the_end_of_the_quiet_hours_that_cover_the_instant() -> None:
    night = person(quiet_start="22:00", quiet_end="07:00")
    assert hold_until(night, utc(5, 23)) == utc(6, 7)
    assert hold_until(night, utc(6, 3)) == utc(6, 7)
    assert hold_until(night, utc(6, 7)) is None  # the end is open: 7:00 sharp is awake
    assert hold_until(night, utc(6, 12)) is None
    day = person(quiet_start="13:00", quiet_end="15:00")  # a window that does not cross midnight
    assert hold_until(day, utc(5, 14)) == utc(5, 15)
    assert hold_until(day, utc(5, 15)) is None
    paris = person(timezone="Europe/Paris", quiet_start="22:00", quiet_end="07:00")
    assert hold_until(paris, utc(5, 21, 30)) == utc(6, 6)  # 22:30 in Paris


@pytest.mark.req("EF-INT-021")
def test_quiet_hours_delay_non_urgent_but_not_the_urgent() -> None:
    p = person(quiet_start="06:00", quiet_end="09:00")
    # the digest hour falls inside the quiet hours: it waits for their end
    assert delivery_time(p, urgent=False, now=utc(5, 3)) == utc(5, 9)
    # an urgent one is held too (a request arrival), unless it is an outage for the admin
    assert delivery_time(p, urgent=True, now=utc(5, 7)) == utc(5, 9)
    assert delivery_time(p, urgent=True, now=utc(5, 7), bypass_quiet=True) == utc(5, 7)
    assert delivery_time(person(), urgent=True, now=utc(5, 7)) == utc(5, 7)  # outside: now
    assert delivery_time(person(), urgent=False, now=utc(5, 3)) == utc(5, 8)
