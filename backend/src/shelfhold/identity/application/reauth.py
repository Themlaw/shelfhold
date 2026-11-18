"""When a session of the identity provider is fresh (M5 L04, EF-USR-024).

A session opened by OpenID Connect is never fresh by itself (L03): no local secret was typed, and
nothing had asked the provider to sign the person in again. To do a reserved action, the session
sends the person back with `prompt=login` and `max_age`, and the answer is believed only when the
ID token says the person signed in there within `max_age` seconds. This module is that one rule,
with no clock of its own.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

# "No local proof of when the person signed in": a session opened by the provider is as old as the
# epoch, so no reserved action that asks for a recent proof is open to it until `reauth` proves it
NEVER = datetime(1970, 1, 1, tzinfo=UTC)
DEFAULT_MAX_AGE_S = 300  # `SHELFHOLD_OIDC_MAX_AGE_S`, bounded by the settings
# A provider whose clock runs ahead of ours by a little is not refused; by more, it is.
FUTURE_SKEW = timedelta(seconds=60)


class FreshnessRefusedError(Exception):
    """The provider's answer does not prove a recent sign-in. `reason` is a code, never a claim."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def check_provider_auth_time(
    auth_time: datetime | None, *, now: datetime, max_age_s: int
) -> datetime:
    """The moment the session is as fresh as: the provider's `auth_time`, never later than `now`.

    Raises `FreshnessRefusedError`: `auth_time_missing` (the claim is absent, so nothing was
    proved), `auth_time_stale` (older than `max_age_s`: the provider answered from its own session
    and ignored `prompt=login`), `auth_time_future` (further ahead than a clock skew)."""
    if auth_time is None or auth_time.tzinfo is None:
        raise FreshnessRefusedError("auth_time_missing")
    when = auth_time.astimezone(UTC)
    if when > now + FUTURE_SKEW:
        raise FreshnessRefusedError("auth_time_future")
    if now - when > timedelta(seconds=max_age_s):
        raise FreshnessRefusedError("auth_time_stale")
    return min(when, now)
