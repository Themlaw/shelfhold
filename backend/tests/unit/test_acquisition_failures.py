"""L18: failure classes and what each one does (EF-SRC-009, EF-SRC-028; 02 section 3.4)."""

import asyncio
from collections.abc import Awaitable
from datetime import timedelta

import pytest

from shelfhold.acquisition.application.failures import from_service
from shelfhold.acquisition.domain.freshness import cadence, stale_threshold
from shelfhold.acquisition.public import Disposition, classify, disposition_of
from shelfhold.infrastructure.fetcher.errors import InvalidFetchUrl
from shelfhold.plugin_host.errors import AuthExpired, AuthModeUnsupported, ServiceVersionMismatch
from shelfhold.sdk.source import (
    AuthRequired,
    ContentRemoved,
    RateLimited,
    SourceChanged,
    TemporarilyUnavailable,
)

pytestmark = pytest.mark.req("EF-SRC-009")


@pytest.mark.parametrize(
    ("error", "failure_class", "disposition"),
    [
        (ContentRemoved("gone"), "content", Disposition.COUNT),
        (AuthExpired("401"), "auth", Disposition.PAUSE),
        (AuthRequired("401"), "auth", Disposition.PAUSE),
        (AuthModeUnsupported(), "auth", Disposition.PAUSE),
        (ServiceVersionMismatch("csrf"), "version", Disposition.PAUSE),
        (SourceChanged("shape"), "schema", Disposition.PAUSE),
        (KeyError("sourceId"), "infra", Disposition.RETRY),  # our own bug: never a pause
        (TypeError("not a dict"), "infra", Disposition.RETRY),
        (TemporarilyUnavailable("asleep"), "infra", Disposition.RETRY),
        (RateLimited("slow down"), "infra", Disposition.RETRY),
        (OSError("reset"), "infra", Disposition.RETRY),
    ],
)
def test_every_failure_has_one_class_and_one_disposition(
    error: Exception, failure_class: str, disposition: Disposition
) -> None:
    assert classify(error) == failure_class
    assert disposition_of(failure_class) is disposition


def test_only_content_failures_are_counted() -> None:
    counted = {c for c in ("content", "auth", "version", "schema", "infra")
               if disposition_of(c) is Disposition.COUNT}  # fmt: skip
    assert counted == {"content"}


def test_cadence_is_the_median_gap_of_distinct_discovery_instants() -> None:
    from datetime import UTC, datetime

    t0 = datetime(2026, 1, 1, tzinfo=UTC)
    days = [0, 0, 0, 7, 14, 28]  # a batch at day 0, then 7, 7, 14
    assert cadence([t0 + timedelta(days=d) for d in days]) == timedelta(days=7)
    assert cadence([t0, t0 + timedelta(days=7)]) is None  # one gap is not a cadence
    assert stale_threshold(None, 3) == timedelta(days=14)
    assert stale_threshold(timedelta(days=7), 3) == timedelta(days=21)


def _boom(error: BaseException) -> Awaitable[None]:
    async def go() -> None:
        raise error

    return go()


@pytest.mark.parametrize(
    "error", [KeyError("sourceId"), TypeError("nodes"), IndexError(0), AttributeError("get")]
)
def test_a_malformed_answer_from_the_service_is_a_schema_change(error: Exception) -> None:
    with pytest.raises(SourceChanged) as raised:
        asyncio.run(from_service(_boom(error)))
    assert classify(raised.value) == "schema"


def test_a_url_the_plugin_hands_back_and_the_fetcher_refuses_is_a_schema_change() -> None:
    with pytest.raises(SourceChanged) as raised:
        asyncio.run(from_service(_boom(InvalidFetchUrl("off origin"))))
    assert classify(raised.value) == "schema"
    assert classify(InvalidFetchUrl("our own call")) == "infra"  # not wrapped: not the plugin


def test_other_errors_pass_through_from_service_untouched() -> None:
    with pytest.raises(ContentRemoved):
        asyncio.run(from_service(_boom(ContentRemoved("gone"))))
