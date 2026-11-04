"""ENF-TIME-001: UTC everywhere, last-write-wins on the server receive time, no future dates."""

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.infrastructure.clock import (
    FutureDateError,
    Stamped,
    SystemClock,
    merge_last_write_wins,
    reject_future,
)
from tests.fakes_clock import ManualClock

T0 = datetime(2026, 11, 4, 12, 0, tzinfo=UTC)


@pytest.mark.req("ENF-TIME-001")
def test_lww_uses_server_receive_timestamp() -> None:
    clock = ManualClock(T0)
    first = merge_last_write_wins(None, "a", clock=clock)
    clock.advance(seconds=5)
    # the second device's own clock is hours behind: its claim does not matter
    second = merge_last_write_wins(first, "b", clock=clock, client_ts=T0 - timedelta(hours=3))
    assert second == Stamped("b", T0 + timedelta(seconds=5))
    assert second.received_at > first.received_at


@pytest.mark.req("ENF-TIME-001")
def test_future_dates_rejected() -> None:
    clock = ManualClock(T0)
    with pytest.raises(FutureDateError):
        merge_last_write_wins(None, "x", clock=clock, client_ts=T0 + timedelta(minutes=6))
    reject_future(T0 + timedelta(minutes=4), now=T0)  # within the default skew
    with pytest.raises(FutureDateError):
        reject_future(T0 + timedelta(days=1), now=T0)
    with pytest.raises(ValueError, match="naive"):
        reject_future(datetime(2026, 1, 1), now=T0)


@pytest.mark.req("ENF-TIME-001")
def test_system_clock_is_aware_utc() -> None:
    assert SystemClock().now().utcoffset() == timedelta(0)
