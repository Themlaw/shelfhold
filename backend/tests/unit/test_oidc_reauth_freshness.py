"""M5 L04: what makes a session of the provider fresh (EF-USR-024): an `auth_time` inside `max_age`.

Pure rule, no clock of its own: the claim, the moment, the age allowed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from shelfhold.identity.application.reauth import (
    DEFAULT_MAX_AGE_S,
    FreshnessRefusedError,
    check_provider_auth_time,
)

pytestmark = pytest.mark.req("EF-USR-024")

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def test_the_default_max_age_is_five_minutes() -> None:
    assert DEFAULT_MAX_AGE_S == 300


def test_oidc_missing_auth_time_refused() -> None:
    with pytest.raises(FreshnessRefusedError) as refused:
        check_provider_auth_time(None, now=NOW, max_age_s=300)
    assert refused.value.reason == "auth_time_missing"


@pytest.mark.parametrize("age", [301, 900, 86400])
def test_an_auth_time_older_than_max_age_is_refused(age: int) -> None:
    with pytest.raises(FreshnessRefusedError) as refused:
        check_provider_auth_time(NOW - timedelta(seconds=age), now=NOW, max_age_s=300)
    assert refused.value.reason == "auth_time_stale"


@pytest.mark.parametrize("age", [0, 1, 299, 300])
def test_an_auth_time_inside_max_age_is_the_freshness_given(age: int) -> None:
    when = NOW - timedelta(seconds=age)
    assert check_provider_auth_time(when, now=NOW, max_age_s=300) == when


def test_a_small_skew_into_the_future_is_clamped_to_now() -> None:
    assert check_provider_auth_time(NOW + timedelta(seconds=30), now=NOW, max_age_s=300) == NOW


def test_an_auth_time_far_in_the_future_is_refused() -> None:
    with pytest.raises(FreshnessRefusedError) as refused:
        check_provider_auth_time(NOW + timedelta(minutes=10), now=NOW, max_age_s=300)
    assert refused.value.reason == "auth_time_future"


def test_a_naive_moment_is_refused_rather_than_guessed() -> None:
    with pytest.raises(FreshnessRefusedError):
        check_provider_auth_time(datetime(2026, 11, 18, 11, 59), now=NOW, max_age_s=300)  # noqa: DTZ001
