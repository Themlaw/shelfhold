"""An injected clock: tests move time by hand, nothing sleeps."""

from datetime import UTC, datetime, timedelta


class ManualClock:
    def __init__(self, start: datetime) -> None:
        assert start.tzinfo is not None
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **delta: float) -> datetime:
        self._now += timedelta(**delta)
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value.astimezone(UTC)
