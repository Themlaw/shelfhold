"""EF-SRC-038: the pure rules of the adaptive check cadence."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.acquisition.application.cadence import CadencePolicy, check_interval, next_check
from shelfhold.config import Settings

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
POLICY = CadencePolicy()


def _weekly(last_ago: timedelta) -> list[datetime]:
    last = NOW - last_ago
    return [last - 14 * DAY, last - 7 * DAY, last]


@pytest.mark.req("EF-SRC-038")
def test_adaptive_check_interval_from_observed_cadence() -> None:
    weekly = _weekly(2 * DAY)
    expected = NOW + 5 * DAY  # the last release plus the observed week
    assert next_check(NOW, weekly, POLICY, finished=False) == expected
    assert HOUR <= next_check(NOW, weekly, POLICY, finished=False) - NOW <= 7 * DAY
    assert check_interval(timedelta(days=3), POLICY) == timedelta(days=3)


@pytest.mark.req("EF-SRC-038")
def test_check_interval_is_never_below_the_minimum_nor_above_the_maximum() -> None:
    """Attack: a burst of releases cannot make the series polled every minute, a yearly one cannot
    push the next look beyond a week."""
    assert check_interval(timedelta(minutes=1), POLICY) == HOUR
    assert check_interval(timedelta(days=400), POLICY) == 7 * DAY
    burst = [NOW - timedelta(minutes=m) for m in (0, 1, 2, 3)]
    assert next_check(NOW, burst, POLICY, finished=False) - NOW >= HOUR
    yearly = [NOW - timedelta(days=d) for d in (0, 365, 730)]
    assert next_check(NOW, yearly, POLICY, finished=False) - NOW <= 7 * DAY
    tight = CadencePolicy(check_min=2 * HOUR, check_max=3 * HOUR)
    assert next_check(NOW, burst, tight, finished=False) - NOW == 2 * HOUR


@pytest.mark.req("EF-SRC-038")
def test_overdue_series_is_looked_at_again_within_the_bounds() -> None:
    late = _weekly(30 * DAY)  # its week has long passed
    wait = next_check(NOW, late, POLICY, finished=False) - NOW
    assert HOUR <= wait <= 7 * DAY


@pytest.mark.req("EF-SRC-038")
def test_unknown_cadence_still_gets_a_bounded_next_check() -> None:
    assert HOUR <= next_check(NOW, [], POLICY, finished=False) - NOW <= 7 * DAY
    assert HOUR <= next_check(NOW, [NOW], POLICY, finished=False) - NOW <= 7 * DAY


@pytest.mark.req("EF-SRC-038")
def test_finished_series_checked_rarely() -> None:
    """Attack: however fast it used to release, a finished series waits the whole 30 days."""
    burst = [NOW - timedelta(minutes=m) for m in (0, 1, 2, 3)]
    assert next_check(NOW, burst, POLICY, finished=True) == NOW + 30 * DAY
    assert next_check(NOW, [], POLICY, finished=True) == NOW + 30 * DAY


@pytest.mark.req("EF-SRC-038")
def test_policy_comes_from_the_settings_and_refuses_inverted_bounds() -> None:
    policy = CadencePolicy.from_settings(Settings())
    assert (policy.check_min, policy.check_max, policy.check_finished) == (HOUR, 7 * DAY, 30 * DAY)
    with pytest.raises(ValueError, match="check_min"):
        Settings(follow_check_min_s=7200, follow_check_max_s=3600)
    with pytest.raises(ValueError):
        Settings(follow_check_min_s=0)
