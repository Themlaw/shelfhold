"""Notices before the dedicated Kavita key expires: J-14 and J-1, once per key (EF-EXP-009)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

THRESHOLDS: Final = (("J14", timedelta(days=14)), ("J1", timedelta(days=1)))


@dataclass(frozen=True)
class KeyNotice:
    threshold: str  # "J14" | "J1"
    days_left: int


@dataclass(frozen=True)
class KeyNoticeState:
    """What was already announced for the key that expires at `expires_at`."""

    expires_at: datetime | None = None
    sent: frozenset[str] = frozenset()


def key_notices(
    expires_at: datetime | None, now: datetime, state: KeyNoticeState
) -> tuple[list[KeyNotice], KeyNoticeState]:
    """The notices due now, and the new state. A new expiry date means a rotated key: start again.

    Reaching J-1 straight away (a short-lived key, a late check) sends one notice, not two.
    """
    if expires_at != state.expires_at:
        state = KeyNoticeState(expires_at)
    if expires_at is None:
        return [], state
    remaining = expires_at - now
    due = [name for name, limit in THRESHOLDS if remaining <= limit]
    if not due or due[-1] in state.sent:
        return [], state
    days_left = max(0, int(remaining.total_seconds() // 86400))
    return [KeyNotice(due[-1], days_left)], KeyNoticeState(expires_at, state.sent | set(due))
