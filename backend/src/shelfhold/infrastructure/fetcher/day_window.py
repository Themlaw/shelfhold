"""The day of a provider's quota (ENF-LEG-005): a calendar day of a named zone, not of UTC.

Google resets the quotas of its APIs at midnight in the Pacific. The window is computed with
`zoneinfo`, so daylight saving time is the library's business: the day of the spring change holds
23 hours and the day of the autumn change 25. Nothing here adds 24 hours to anything.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

__all__ = ["day_key", "midnight_after", "next_midnight"]


def day_key(timestamp: float, zone: str | None = None) -> str:
    """The calendar day (`YYYY-MM-DD`) of an instant in `zone`; UTC when none is given."""
    tz = UTC if zone is None else ZoneInfo(zone)
    return datetime.fromtimestamp(timestamp, tz).date().isoformat()


def next_midnight(timestamp: float, zone: str) -> float:
    """The first instant of the day that follows the one `timestamp` is in, in `zone`."""
    tz = ZoneInfo(zone)
    today = datetime.fromtimestamp(timestamp, tz).date()
    # the wall-clock midnight of tomorrow, then read back as an instant: the offset that holds
    # at that moment (not today's) is the one `zoneinfo` applies
    midnight = datetime.combine(today + timedelta(days=1), time.min, tzinfo=tz)
    return midnight.timestamp()


def midnight_after(now: datetime, zone: str) -> datetime:
    """The next midnight of `zone` after `now` (an aware instant), as a UTC instant."""
    if now.tzinfo is None:
        raise ValueError("an aware instant is required")
    return datetime.fromtimestamp(next_midnight(now.timestamp(), zone), UTC)
