"""L13 (ENF-LEG-005, ENF-LEG-007): what the host does around a metadata provider.

The identifying `User-Agent` (with a contact only when the instance configured one), the token
bucket of each provider (the host's, never the plugin's), the cache a provider cannot do without
and the robots.txt it respects. A fake OpenLibrary answers; nothing leaves the machine.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable

import pytest

from shelfhold import __version__
from shelfhold.config import Settings
from shelfhold.infrastructure.fetcher import GuardedFetcher, RobotsDisallowed
from shelfhold.infrastructure.fetcher.client import (
    USER_AGENT,
    identifiable_user_agent,
    is_identifiable,
)
from shelfhold.infrastructure.fetcher.errors import ProviderQuotaExceeded, UserAgentNotIdentifiable
from shelfhold.infrastructure.fetcher.limiter import ProviderBuckets, TokenBucket
from shelfhold.plugin_host.adapters.native.openlibrary import build_manifest, quota_for
from shelfhold.plugin_host.provider_fetcher import ProviderFetcher, ProviderRuntime, open_provider
from shelfhold.sdk.metadata import ProviderQuota
from tests.fakes.openlibrary.server import API_HOST, ISBN, FakeOpenLibrary

CONTACT = "owner@example.org"
BOOKS = f"https://{API_HOST}/api/books?bibkeys=ISBN:{ISBN}&format=json&jscmd=data"


def run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class FakeTime:
    """The bucket's clock and sleep: waiting moves the clock, nobody really waits."""

    def __init__(self) -> None:
        self.t = 1_000.0
        self.slept: list[float] = []

    def now(self) -> float:
        return self.t

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds


def provider_fetcher(
    fake: FakeOpenLibrary, *, contact: str | None = None, time: FakeTime | None = None,
    runtime: ProviderRuntime | None = None,
) -> ProviderFetcher:  # fmt: skip
    time = time or FakeTime()
    fake.clock = time.now
    runtime = runtime or ProviderRuntime(clock=time.now, sleep=time.sleep)
    return open_provider(
        build_manifest(), fake.resolver, contact=contact, runtime=runtime, transport=fake.transport
    )


# -- the identifying agent ---------------------------------------------------------------------


@pytest.mark.req("ENF-LEG-005", "ENF-LEG-007")
def test_openlibrary_user_agent_with_contact() -> None:
    fake = FakeOpenLibrary()
    run(provider_fetcher(fake, contact=CONTACT).get(BOOKS))
    (seen,) = fake.api_calls()
    assert seen.user_agent == identifiable_user_agent(CONTACT)
    assert seen.user_agent is not None and CONTACT in seen.user_agent
    assert seen.user_agent.startswith(f"Shelfhold/{__version__} (+https://")
    assert fake.requests_to(API_HOST, "/robots.txt")[0].user_agent == seen.user_agent


@pytest.mark.req("ENF-LEG-005", "ENF-LEG-007")
def test_default_user_agent_has_the_repository_and_never_an_address() -> None:
    assert identifiable_user_agent(None) == USER_AGENT
    assert "@" not in USER_AGENT and "https://github.com/Themlaw/shelfhold" in USER_AGENT
    fake = FakeOpenLibrary()
    run(provider_fetcher(fake).get(BOOKS))
    assert fake.api_calls()[0].user_agent == USER_AGENT


@pytest.mark.req("ENF-LEG-005")
@pytest.mark.parametrize("contact", ["a b@example.org", "x@y)", "(x@example.org", "x@y;z", "",
                                      "http://example.org/contact", "no-at-no-url", "a@b\r\nX: 1"])
def test_a_contact_that_could_break_the_agent_is_refused(contact: str) -> None:
    with pytest.raises(ValueError, match="contact"):
        identifiable_user_agent(contact)
    with pytest.raises(ValueError):
        Settings(metadata_contact=contact)


@pytest.mark.req("ENF-LEG-005")
def test_a_provider_is_never_called_with_an_anonymous_user_agent() -> None:
    """A Fetcher whose agent identifies nobody (the library's own, a browser's) is refused before
    any request leaves."""
    fake = FakeOpenLibrary()
    for agent in ("python-httpx/0.27", "Mozilla/5.0 (X11; Linux x86_64)", "", "Shelfhold"):
        assert not is_identifiable(agent)
        inner = GuardedFetcher(fake.resolver, transport=fake.transport, user_agent=agent)
        with pytest.raises(UserAgentNotIdentifiable):
            ProviderFetcher(inner, build_manifest(), ProviderRuntime())
    assert fake.seen == []
    assert is_identifiable(USER_AGENT) and is_identifiable(identifiable_user_agent(CONTACT))


@pytest.mark.req("ENF-LEG-005")
def test_a_plugin_cannot_replace_the_user_agent() -> None:
    fake = FakeOpenLibrary()
    f = provider_fetcher(fake, contact=CONTACT)
    run(f.get(BOOKS, headers={"User-Agent": "python-requests/2", "user-agent": "evil"}))
    assert {s.user_agent for s in fake.seen} == {identifiable_user_agent(CONTACT)}


# -- the token bucket --------------------------------------------------------------------------


@pytest.mark.req("ENF-LEG-005")
def test_metadata_token_bucket_per_provider() -> None:
    """One request per second without a contact: ten calls in a loop reach the server one second
    apart, however fast the plugin asks."""
    fake = FakeOpenLibrary()
    time = FakeTime()
    f = provider_fetcher(fake, time=time)
    for _ in range(6):
        run(f.get(BOOKS, headers={"X-Nonce": str(len(fake.seen))}))  # not the same cache key
    at = [s.at for s in fake.api_calls()]
    assert len(at) == 6
    assert all(b - a >= 1.0 - 1e-9 for a, b in zip(at, at[1:], strict=False))
    assert quota_for(None).requests_per_second == 1.0


@pytest.mark.req("ENF-LEG-005")
def test_contact_opens_three_requests_per_second_and_no_more() -> None:
    assert quota_for(CONTACT).requests_per_second == 3.0
    fake = FakeOpenLibrary()
    time = FakeTime()
    f = provider_fetcher(fake, contact=CONTACT, time=time)
    for n in range(9):
        run(f.get(BOOKS, headers={"X-Nonce": str(n)}))
    api = [s.at for s in fake.api_calls()]
    assert max(b - a for a, b in zip(api, api[1:], strict=False)) <= 1 / 3 + 1e-6
    assert all(b - a >= 1 / 3 - 1e-9 for a, b in zip(api, api[1:], strict=False))


@pytest.mark.req("ENF-LEG-005")
def test_the_bucket_counts_every_request_even_robots_and_redirect_hops() -> None:
    """robots.txt and the books call are two requests: the second one waits."""
    fake = FakeOpenLibrary()
    time = FakeTime()
    run(provider_fetcher(fake, time=time).get(BOOKS))
    assert len(fake.seen) == 2  # robots.txt, then the call
    assert fake.seen[1].at - fake.seen[0].at >= 1.0 - 1e-9


@pytest.mark.req("ENF-LEG-005")
def test_the_bucket_is_the_hosts_and_shared_by_every_fetcher_of_the_provider() -> None:
    """A plugin that opens many Fetchers (or many calls at once) still shares one bucket."""
    fake = FakeOpenLibrary()
    time = FakeTime()
    runtime = ProviderRuntime(clock=time.now, sleep=time.sleep)
    first = provider_fetcher(fake, time=time, runtime=runtime)
    second = provider_fetcher(fake, time=time, runtime=runtime)

    async def both() -> None:
        await asyncio.gather(
            first.get(BOOKS, headers={"X-Nonce": "1"}),
            second.get(BOOKS, headers={"X-Nonce": "2"}),
            first.get(BOOKS, headers={"X-Nonce": "3"}),
        )

    run(both())
    at = sorted(s.at for s in fake.api_calls())
    assert len(at) == 3 and all(b - a >= 1.0 - 1e-9 for a, b in zip(at, at[1:], strict=False))


@pytest.mark.req("ENF-LEG-005")
def test_a_burst_above_the_bucket_waits_and_a_daily_cap_refuses() -> None:
    time = FakeTime()
    bucket = TokenBucket(rate=2.0, burst=2, daily_cap=3, clock=time.now, sleep=time.sleep,
                         wall=time.now)  # fmt: skip

    async def go() -> None:
        for _ in range(3):
            await bucket.acquire()
        assert time.slept == [pytest.approx(0.5)]  # two at once, the third waited
        with pytest.raises(ProviderQuotaExceeded):
            await bucket.acquire()  # the 4th of the day: refused, not delayed
        time.t += 86_400
        await bucket.acquire()  # the next day

    run(go())


@pytest.mark.req("ENF-LEG-005")
def test_a_refused_request_does_not_leave_the_host() -> None:
    fake = FakeOpenLibrary()
    time = FakeTime()
    manifest = build_manifest().model_copy(
        update={"quota": ProviderQuota(requests_per_second=100.0, burst=100, daily_cap=2)}
    )
    runtime = ProviderRuntime(clock=time.now, sleep=time.sleep)
    fake.clock = time.now
    f = open_provider(manifest, fake.resolver, contact=None, runtime=runtime,
                      transport=fake.transport)  # fmt: skip
    run(f.get(f"https://{API_HOST}/api/books?bibkeys=ISBN:1"))  # robots.txt and the call: 2
    # a new URL is not in the cache: it is refused before it is sent
    sent = len(fake.seen)
    with pytest.raises(ProviderQuotaExceeded):
        run(f.get(f"https://{API_HOST}/api/books?bibkeys=ISBN:2"))
    assert len(fake.seen) == sent


@pytest.mark.req("ENF-LEG-005")
def test_the_buckets_change_when_a_contact_is_configured() -> None:
    buckets = ProviderBuckets()
    slow = buckets.bucket_for("openlibrary", quota_for(None))
    assert buckets.bucket_for("openlibrary", quota_for(None)) is slow  # one per provider
    fast = buckets.bucket_for("openlibrary", quota_for(CONTACT))
    assert fast is not slow and fast.rate == 3.0
    assert buckets.bucket_for("another", quota_for(None)) is not slow


# -- the cache and robots ----------------------------------------------------------------------


@pytest.mark.req("ENF-LEG-007", "EF-BIB-013")
def test_metadata_cache_hit() -> None:
    fake = FakeOpenLibrary(cache_control="max-age=3600")
    time = FakeTime()
    f = provider_fetcher(fake, time=time)
    first, second = run(f.get(BOOKS)), run(f.get(BOOKS))
    assert first.body == second.body and len(fake.api_calls()) == 1
    assert time.slept == []  # a cached answer costs no token


@pytest.mark.req("ENF-LEG-007")
def test_a_stale_answer_is_revalidated_and_the_304_costs_a_request_not_a_body() -> None:
    fake = FakeOpenLibrary(etag='"v1"')
    f = provider_fetcher(fake)
    run(f.get(BOOKS))
    again = run(f.get(BOOKS))
    assert again.status == 200 and again.body  # the 304 became the stored 200
    calls = fake.api_calls()
    assert len(calls) == 2 and calls[1].headers["if-none-match"] == '"v1"'


@pytest.mark.req("ENF-LEG-007")
def test_the_cache_stays_on_when_the_instance_turns_it_off() -> None:
    """`fetcher_cache = false` is for the sources: a metadata provider keeps its cache (it is the
    provider's condition), and so does a provider listed in `fetcher_cache_off_sources`."""
    settings = Settings(fetcher_cache=False, fetcher_cache_off_sources=("openlibrary",))
    assert settings.fetcher_cache is False
    fake = FakeOpenLibrary(cache_control="max-age=3600")
    f = provider_fetcher(fake)
    run(f.get(BOOKS))
    run(f.get(BOOKS))
    assert len(fake.api_calls()) == 1


@pytest.mark.req("ENF-LEG-007")
def test_an_authenticated_request_is_never_cached() -> None:
    fake = FakeOpenLibrary(cache_control="max-age=3600")
    f = provider_fetcher(fake)
    run(f.get(BOOKS, headers={"Authorization": "Bearer synthetic"}))
    run(f.get(BOOKS, headers={"Authorization": "Bearer synthetic"}))
    assert len(fake.api_calls()) == 2


@pytest.mark.req("ENF-LEG-007", "ENF-LEG-003")
def test_robots_respected() -> None:
    fake = FakeOpenLibrary(robots={API_HOST: "User-agent: *\nDisallow: /api/\n"})
    f = provider_fetcher(fake)
    with pytest.raises(RobotsDisallowed):
        run(f.get(BOOKS))
    assert fake.api_calls() == []  # the disallowed path was never requested
    assert build_manifest().respects_robots is True


@pytest.mark.req("ENF-LEG-007", "ENF-LEG-003")
def test_robots_that_name_shelfhold_apply_and_a_missing_file_allows_everything() -> None:
    fake = FakeOpenLibrary(robots={API_HOST: "User-agent: Shelfhold\nDisallow: /\n"})
    with pytest.raises(RobotsDisallowed):
        run(provider_fetcher(fake).get(BOOKS))
    open_fake = FakeOpenLibrary()  # no robots.txt: 404
    assert run(provider_fetcher(open_fake).get(BOOKS)).status == 200


@pytest.mark.req("ENF-SEC-001")
def test_a_provider_reaches_only_its_declared_domains() -> None:
    from shelfhold.infrastructure.fetcher import DomainNotAllowed

    fake = FakeOpenLibrary()
    f = provider_fetcher(fake)
    with pytest.raises(DomainNotAllowed):
        run(f.get("https://elsewhere.example.net/x"))
    assert fake.seen == []
