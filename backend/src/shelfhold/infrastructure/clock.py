"""Clocks (ENF-TIME-001): UTC in the database, the server's receive time arbitrates.

A client's own timestamp is a claim, never an ordering key: the stamp that counts is the one the
server takes when the write arrives. A claim from the future is refused outright. Components take a
`Clock` so that tests move time by hand instead of sleeping.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

DEFAULT_SKEW = timedelta(minutes=5)  # tolerated drift between a client's clock and ours


class Clock(Protocol):
    def now(self) -> datetime:
        """The current instant, timezone-aware, in UTC."""
        ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FutureDateError(ValueError):
    """A client claims an instant later than the server's own clock allows."""


def reject_future(value: datetime, *, now: datetime, skew: timedelta = DEFAULT_SKEW) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime: pass a timezone-aware value")
    if value > now + skew:
        raise FutureDateError(f"{value.isoformat()} is in the future")


@dataclass(frozen=True)
class Stamped[T]:
    value: T
    received_at: datetime


def merge_last_write_wins[T](
    current: Stamped[T] | None,
    value: T,
    *,
    clock: Clock,
    client_ts: datetime | None = None,
) -> Stamped[T]:
    """Stamp `value` with the server's receive time; `client_ts` is only checked, never used."""
    received_at = clock.now()
    if client_ts is not None:
        reject_future(client_ts, now=received_at)
    if current is not None and current.received_at > received_at:
        return current  # the server clock went back: keep the write that was stamped later
    return Stamped(value, received_at)
