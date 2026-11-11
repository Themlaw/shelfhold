"""ENF-LEG-007, ENF-LEG-003: conditional GET, RFC 9111 cache, robots, identifiable UA."""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable

import httpx2
import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.fetcher import (
    DomainNotAllowed,
    GuardedFetcher,
    RobotsDisallowed,
)
from shelfhold.infrastructure.fetcher.cache import HttpCache, cache_for
from shelfhold.infrastructure.fetcher.client import USER_AGENT
from shelfhold.infrastructure.fetcher.guard import DomainAllowlist
from shelfhold.infrastructure.fetcher.robots import RobotsPolicy
from shelfhold.sdk.models import DomainRule
from tests.fakes_fetcher import FakeResolver, Recorder

PUBLIC = "93.184.216.34"
HOST = "books.example.org"
PAGE = f"https://{HOST}/a/page"


def _run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


class Clock:
    def __init__(self) -> None:
        self.t = 1_000_000.0

    def __call__(self) -> float:
        return self.t


def _allow(source: str, pattern: str = HOST) -> DomainAllowlist:
    return DomainAllowlist(source, [DomainRule(pattern=pattern, purpose="api")])


def _fetcher(
    rec: Recorder,
    *,
    cache: HttpCache | None = None,
    robots: RobotsPolicy | None = None,
    source: str = "src-a",
    hosts: tuple[str, ...] = (HOST,),
) -> GuardedFetcher:
    resolver = FakeResolver({h: [PUBLIC] for h in hosts})
    return GuardedFetcher(
        resolver, transport=rec.transport, domains=_allow(source), cache=cache, robots=robots
    )


def _server(etag: str = '"v1"', **extra: str) -> Recorder:
    """An origin that honours If-None-Match and counts what it served."""

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("if-none-match") == etag:
            return httpx2.Response(304, headers={"etag": etag, **extra})
        return httpx2.Response(200, content=b"body", headers={"etag": etag, **extra})

    return Recorder(respond)


@pytest.mark.req("ENF-LEG-007")
def test_conditional_get_304_reuses_cache() -> None:
    rec = _server()
    f = _fetcher(rec, cache=HttpCache(clock=Clock()))
    first = _run(f.get(PAGE))
    second = _run(f.get(PAGE))
    assert first.status == 200 and first.body == b"body"
    assert second.status == 200 and second.body == b"body"  # the 304 is turned into the cached 200
    assert len(rec.requests) == 2
    assert "if-none-match" not in rec.requests[0].headers
    assert rec.requests[1].headers["if-none-match"] == '"v1"'


@pytest.mark.req("ENF-LEG-007")
def test_conditional_get_uses_last_modified() -> None:
    lm = "Wed, 21 Oct 2015 07:28:00 GMT"

    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.headers.get("if-modified-since") == lm:
            return httpx2.Response(304)
        return httpx2.Response(200, content=b"lm", headers={"last-modified": lm})

    rec = Recorder(respond)
    f = _fetcher(rec, cache=HttpCache(clock=Clock()))
    _run(f.get(PAGE))
    assert _run(f.get(PAGE)).body == b"lm"
    assert rec.requests[1].headers["if-modified-since"] == lm


@pytest.mark.req("ENF-LEG-007")
def test_fresh_entry_is_served_without_a_request_until_it_expires() -> None:
    clock = Clock()
    rec = _server(**{"cache-control": "max-age=60"})
    f = _fetcher(rec, cache=HttpCache(clock=clock))
    _run(f.get(PAGE))
    clock.t += 30
    assert _run(f.get(PAGE)).body == b"body"
    assert len(rec.requests) == 1
    clock.t += 31
    assert _run(f.get(PAGE)).body == b"body"
    assert len(rec.requests) == 2 and "if-none-match" in rec.requests[1].headers


@pytest.mark.req("ENF-LEG-007")
def test_no_cache_entry_is_always_revalidated() -> None:
    rec = _server(**{"cache-control": "no-cache, max-age=600"})
    f = _fetcher(rec, cache=HttpCache(clock=Clock()))
    _run(f.get(PAGE))
    _run(f.get(PAGE))
    assert len(rec.requests) == 2 and "if-none-match" in rec.requests[1].headers


@pytest.mark.req("ENF-LEG-007")
@pytest.mark.parametrize(
    "headers",
    [
        {"cache-control": "no-store"},
        {"cache-control": "private, max-age=600"},
        {"vary": "*", "cache-control": "max-age=600"},
        {"set-cookie": "a=1", "cache-control": "max-age=600"},
    ],
)
def test_responses_that_forbid_storing_are_not_stored(headers: dict[str, str]) -> None:
    rec = Recorder(lambda r: httpx2.Response(200, content=b"x", headers={"etag": '"e"', **headers}))
    f = _fetcher(rec, cache=HttpCache(clock=Clock()))
    _run(f.get(PAGE))
    _run(f.get(PAGE))
    assert len(rec.requests) == 2
    assert all("if-none-match" not in r.headers for r in rec.requests)


@pytest.mark.req("ENF-LEG-007")
def test_vary_is_respected() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        lang = request.headers.get("accept-language", "")
        return httpx2.Response(
            200,
            content=lang.encode(),
            headers={"vary": "Accept-Language", "cache-control": "max-age=600"},
        )

    rec = Recorder(respond)
    f = _fetcher(rec, cache=HttpCache(clock=Clock()))
    assert _run(f.get(PAGE, headers={"Accept-Language": "fr"})).body == b"fr"
    assert _run(f.get(PAGE, headers={"Accept-Language": "en"})).body == b"en"
    assert _run(f.get(PAGE, headers={"Accept-Language": "fr"})).body == b"fr"
    assert len(rec.requests) == 2  # the second language missed, the third call hit


@pytest.mark.req("ENF-LEG-007", "ENF-SEC-001")
def test_authenticated_requests_never_reach_the_shared_cache() -> None:
    n = 0

    def respond(request: httpx2.Request) -> httpx2.Response:
        nonlocal n
        n += 1
        who = request.headers.get("authorization", "anon")
        return httpx2.Response(200, content=who.encode(), headers={"cache-control": "max-age=600"})

    rec = Recorder(respond)
    f = _fetcher(rec, cache=HttpCache(clock=Clock()))
    assert _run(f.get(PAGE, headers={"Authorization": "tok-1"})).body == b"tok-1"
    assert _run(f.get(PAGE, headers={"Authorization": "tok-2"})).body == b"tok-2"
    assert _run(f.get(PAGE)).body == b"anon"  # nothing stored by the account calls
    assert _run(f.get(PAGE, headers={"Authorization": "tok-1"})).body == b"tok-1"
    assert n == 4
    _run(f.get(PAGE, headers={"Cookie": "s=1"}))
    assert _run(f.get(PAGE, headers={"Cookie": "s=2"})).body == b"anon"
    assert n == 6


@pytest.mark.req("ENF-LEG-007", "ENF-SEC-001")
def test_cache_never_serves_another_source() -> None:
    cache = HttpCache(clock=Clock())
    rec = Recorder(
        lambda r: httpx2.Response(200, content=b"x", headers={"cache-control": "max-age=600"})
    )
    _run(_fetcher(rec, cache=cache, source="src-a").get(PAGE))
    _run(_fetcher(rec, cache=cache, source="src-b").get(PAGE))
    assert len(rec.requests) == 2  # same shared cache object, same URL: still a miss


@pytest.mark.req("ENF-LEG-007", "EF-SRC-003")
def test_domain_guard_applies_before_a_cache_hit() -> None:
    cache = HttpCache(clock=Clock())
    rec = Recorder(
        lambda r: httpx2.Response(200, content=b"x", headers={"cache-control": "max-age=600"})
    )
    _run(_fetcher(rec, cache=cache, source="src-a").get(PAGE))
    resolver = FakeResolver({HOST: [PUBLIC]})
    narrowed = GuardedFetcher(
        resolver,
        transport=rec.transport,
        domains=_allow("src-a", "other.example.org"),
        cache=cache,
    )
    with pytest.raises(DomainNotAllowed):
        _run(narrowed.get(PAGE))
    assert len(rec.requests) == 1


@pytest.mark.req("ENF-LEG-007")
def test_a_redirected_answer_is_not_cached_under_the_first_url() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.headers["host"] == HOST and request.url.path == "/a/page":
            return httpx2.Response(302, headers={"location": f"https://{HOST}/b/final"})
        return httpx2.Response(200, content=b"x", headers={"cache-control": "max-age=600"})

    rec = Recorder(respond)
    f = _fetcher(rec, cache=HttpCache(clock=Clock()))
    _run(f.get(PAGE))
    _run(f.get(PAGE))
    assert len(rec.requests) == 4  # two hops each time: the final answer was not filed under PAGE


@pytest.mark.req("ENF-LEG-007")
def test_unsafe_method_invalidates_the_entry() -> None:
    rec = Recorder(
        lambda r: httpx2.Response(200, content=b"x", headers={"cache-control": "max-age=600"})
    )
    f = _fetcher(rec, cache=HttpCache(clock=Clock()))
    _run(f.get(PAGE))
    _run(f.post(PAGE, data={"a": "b"}))
    _run(f.get(PAGE))
    assert [r.method for r in rec.requests] == ["GET", "POST", "GET"]


@pytest.mark.req("ENF-LEG-007")
def test_cache_disabled_per_source() -> None:
    settings = Settings(fetcher_cache=True, fetcher_cache_off_sources=("src-off",))
    assert cache_for("src-off", settings.fetcher_cache, settings.fetcher_cache_off_sources) is None
    assert cache_for("src-on", settings.fetcher_cache, settings.fetcher_cache_off_sources)
    assert Settings(fetcher_cache=False).fetcher_cache is False
    assert cache_for("src-on", False, ()) is None
    rec = _server()
    f = _fetcher(rec, cache=cache_for("src-off", True, ("src-off",)))
    _run(f.get(PAGE))
    _run(f.get(PAGE))
    assert len(rec.requests) == 2
    assert all("if-none-match" not in r.headers for r in rec.requests)


def _site(robots: str | int = "User-agent: *\nDisallow: /private/\n") -> Recorder:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/robots.txt":
            if isinstance(robots, int):
                return httpx2.Response(robots)
            return httpx2.Response(200, content=robots.encode())
        return httpx2.Response(200, content=b"page")

    return Recorder(respond)


@pytest.mark.req("ENF-LEG-007", "ENF-LEG-003")
def test_robots_respected_for_native_sources() -> None:
    rec = _site()
    f = _fetcher(rec, robots=RobotsPolicy(clock=Clock()))
    assert _run(f.get(f"https://{HOST}/public/x")).body == b"page"
    with pytest.raises(RobotsDisallowed):
        _run(f.get(f"https://{HOST}/private/x"))
    assert [r.url.path for r in rec.requests] == ["/robots.txt", "/public/x"]  # read once, cached


@pytest.mark.req("ENF-LEG-003")
def test_robots_not_consulted_when_the_source_does_not_respect_it() -> None:
    rec = _site()
    f = _fetcher(rec, robots=None)  # a source with respects_robots = false gets no policy
    assert _run(f.get(f"https://{HOST}/private/x")).body == b"page"
    assert [r.url.path for r in rec.requests] == ["/private/x"]


@pytest.mark.req("ENF-LEG-003")
def test_robots_absent_allows_and_unavailable_disallows() -> None:
    assert _run(_fetcher(_site(404), robots=RobotsPolicy(clock=Clock())).get(PAGE)).status == 200
    with pytest.raises(RobotsDisallowed):
        _run(_fetcher(_site(503), robots=RobotsPolicy(clock=Clock())).get(PAGE))


@pytest.mark.req("ENF-LEG-003")
def test_robots_checked_at_every_hop() -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/robots.txt":
            return httpx2.Response(200, content=b"User-agent: *\nDisallow: /private/\n")
        if request.url.path == "/go":
            return httpx2.Response(302, headers={"location": f"https://{HOST}/private/z"})
        return httpx2.Response(200, content=b"page")

    rec = Recorder(respond)
    f = _fetcher(rec, robots=RobotsPolicy(clock=Clock()))
    with pytest.raises(RobotsDisallowed):
        _run(f.get(f"https://{HOST}/go"))
    assert "/private/z" not in [r.url.path for r in rec.requests]


@pytest.mark.req("ENF-LEG-003")
def test_robots_group_for_the_shelfhold_agent_wins() -> None:
    body = "User-agent: *\nDisallow:\n\nUser-agent: Shelfhold\nDisallow: /\n"
    with pytest.raises(RobotsDisallowed):
        _run(_fetcher(_site(body), robots=RobotsPolicy(clock=Clock())).get(PAGE))


@pytest.mark.req("ENF-LEG-007", "ENF-LEG-003")
def test_user_agent_identifiable() -> None:
    assert re.fullmatch(
        r"Shelfhold/\d+\.\d+\S* \(\+https://github\.com/Themlaw/shelfhold\)", USER_AGENT
    )
    rec = _site()
    f = _fetcher(rec, robots=RobotsPolicy(clock=Clock()))
    _run(f.get(PAGE))
    assert {r.headers["user-agent"] for r in rec.requests} == {USER_AGENT}  # robots.txt too
