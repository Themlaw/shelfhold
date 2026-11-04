"""PR 32 review: regressions for the LAN springboard, credential leaks, error mapping and TLS."""

from __future__ import annotations

import asyncio
import gzip
import ssl
from collections.abc import AsyncIterator, Awaitable

import httpx2
import pytest

from shelfhold.infrastructure.fetcher import (
    FetchError,
    FetchLimits,
    GuardedFetcher,
    IntegrationFetcher,
    LanTargets,
    SsrfBlocked,
    UnsafeRedirect,
)
from tests.fakes_fetcher import FakeResolver, Recorder

PUBLIC = "93.184.216.34"


def _run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


def _lan(dns: FakeResolver, *entries: str) -> LanTargets:
    return _run(LanTargets.declare(list(entries), dns))


def _dns() -> FakeResolver:
    return FakeResolver(
        {
            "up.example.org": [PUBLIC],
            "kavita.lan": ["192.168.1.20"],
            "suwayomi.lan": ["192.168.1.21"],
        }
    )


def _redirecting(location: str, status: int = 302) -> Recorder:
    def respond(r: httpx2.Request) -> httpx2.Response:
        if len(rec.requests) == 1:
            return httpx2.Response(status, headers={"location": location})
        return httpx2.Response(200, content=b"ok")

    rec = Recorder(respond)
    return rec


@pytest.mark.req("ENF-SEC-001")
def test_lan_target_is_not_a_springboard_for_a_redirect_from_an_upstream_origin() -> None:
    dns = _dns()
    lan = _lan(dns, "kavita.lan:8080")
    rec = _redirecting("http://kavita.lan:8080/admin")
    f = IntegrationFetcher(dns, lan_targets=lan, transport=rec.transport)
    with pytest.raises(SsrfBlocked):
        _run(f.get("http://up.example.org/"))
    assert rec.hosts == [PUBLIC]  # the LAN address was never contacted


@pytest.mark.req("ENF-SEC-001")
def test_default_fetcher_has_no_lan_target_at_all() -> None:
    dns = _dns()
    rec = Recorder()
    with pytest.raises(TypeError):
        GuardedFetcher(dns, lan_targets=_lan(dns, "kavita.lan:8080"))  # type: ignore[call-arg]
    f = GuardedFetcher(dns, transport=rec.transport)
    for url in ("http://kavita.lan:8080/", "http://192.168.1.20:8080/"):
        with pytest.raises(SsrfBlocked):
            _run(f.get(url))
    assert rec.requests == []


@pytest.mark.req("ENF-SEC-001")
def test_integration_fetcher_reaches_its_lan_target_but_the_default_one_does_not() -> None:
    # the same declaration, the same URL: only the caller that was built with the target gets in
    dns = _dns()
    lan = _lan(dns, "kavita.lan:8080")
    rec = Recorder()
    _run(IntegrationFetcher(dns, lan_targets=lan, transport=rec.transport).get("http://kavita.lan:8080/"))
    assert rec.hosts == ["192.168.1.20"]
    with pytest.raises(SsrfBlocked):
        _run(GuardedFetcher(dns, transport=Recorder().transport).get("http://kavita.lan:8080/"))


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize(
    "location", ["http://suwayomi.lan:8081/", "http://up.example.org/", "https://kavita.lan:8080/"]
)
def test_redirect_from_a_lan_target_never_leaves_its_origin(location: str) -> None:
    dns = _dns()
    lan = _lan(dns, "kavita.lan:8080", "suwayomi.lan:8081")
    rec = _redirecting(location)
    f = IntegrationFetcher(dns, lan_targets=lan, transport=rec.transport)
    with pytest.raises(SsrfBlocked):
        _run(f.get("http://kavita.lan:8080/"))
    assert len(rec.requests) == 1


@pytest.mark.req("ENF-SEC-001")
def test_redirect_inside_a_lan_target_origin_is_allowed() -> None:
    dns = _dns()
    rec = _redirecting("/login")
    f = IntegrationFetcher(dns, lan_targets=_lan(dns, "kavita.lan:8080"), transport=rec.transport)
    assert _run(f.get("http://kavita.lan:8080/")).status == 200
    assert rec.requests[1].url.path == "/login"


@pytest.mark.req("ENF-SEC-001")
def test_off_origin_redirect_forwards_only_the_header_allow_list() -> None:
    dns = _dns()
    dns.table["other.example.org"] = ["93.184.216.35"]
    rec = _redirecting("http://other.example.org/")
    headers = {
        "Accept": "a/b", "Accept-Language": "fr", "Accept-Encoding": "gzip", "User-Agent": "u",
        "X-Session-Id": "1", "Authorization": "t", "Referer": "http://up.example.org/p",
        "X-Whatever": "1", "Cf-Access-Jwt-Assertion": "j",
    }  # fmt: skip
    _run(GuardedFetcher(dns, transport=rec.transport).get("http://up.example.org/", headers=headers))
    sent = rec.requests[1].headers
    assert {k for k in sent.keys()} <= {"accept", "accept-language", "accept-encoding", "user-agent", "host"}
    assert sent["accept"] == "a/b"


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize("status", [307, 308])
def test_307_and_308_across_origins_are_refused(status: int) -> None:
    dns = _dns()
    dns.table["other.example.org"] = ["93.184.216.35"]
    rec = _redirecting("http://other.example.org/", status)
    f = GuardedFetcher(dns, transport=rec.transport)
    with pytest.raises(UnsafeRedirect):
        _run(f.post("http://up.example.org/", json={"password": "x"}))
    assert len(rec.requests) == 1


@pytest.mark.req("ENF-SEC-001")
def test_httpx_errors_surface_as_fetch_errors() -> None:
    dns = _dns()

    async def broken() -> AsyncIterator[bytes]:
        yield b"partial"
        raise httpx2.ReadError("connection reset")

    cases = [
        Recorder(lambda r: httpx2.Response(200, content=broken())),  # reset mid-body
        Recorder(lambda r: httpx2.Response(200, headers={"content-encoding": "gzip"}, content=b"no")),
        Recorder(lambda r: httpx2.Response(200, headers={"content-encoding": "gzip"},
                                           content=gzip.compress(b"x" * 100)[:-9])),  # truncated
    ]  # fmt: skip
    for rec in cases:
        with pytest.raises(FetchError):
            _run(GuardedFetcher(dns, transport=rec.transport).get("http://up.example.org/"))

    def raising(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.RemoteProtocolError("bad framing")

    with pytest.raises(FetchError):
        _run(GuardedFetcher(dns, transport=httpx2.MockTransport(raising)).get("http://up.example.org/"))


@pytest.mark.req("ENF-SEC-001")
def test_constructor_refuses_a_tls_context_that_does_not_verify() -> None:
    dns = _dns()
    no_check = ssl.create_default_context()
    no_check.check_hostname = False
    no_verify = ssl.create_default_context()
    no_verify.check_hostname = False
    no_verify.verify_mode = ssl.CERT_NONE
    for context in (no_check, no_verify):
        with pytest.raises(ValueError, match="verif"):
            GuardedFetcher(dns, tls_context=context)
        with pytest.raises(ValueError, match="verif"):
            IntegrationFetcher(dns, lan_targets=LanTargets.empty(), tls_context=context)
    GuardedFetcher(dns, tls_context=ssl.create_default_context())


_ = FetchLimits
