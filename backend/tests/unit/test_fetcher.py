"""ENF-SEC-001 / EF-UX-003: the Fetcher end to end over a mock transport and a scripted DNS."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable

import httpx2
import pytest

from shelfhold.infrastructure.fetcher import (
    DnsBlocked,
    FetchLimits,
    FetchTimeout,
    FetchTooLarge,
    GuardedFetcher,
    IntegrationFetcher,
    InvalidFetchUrl,
    LanTargets,
    SsrfBlocked,
    TooManyRedirects,
)
from tests.fakes_fetcher import FakeResolver, Recorder

PUBLIC = "93.184.216.34"


def _run[T](coro: Awaitable[T]) -> T:
    async def go() -> T:
        return await coro

    return asyncio.run(go())


def _fetcher(
    resolver: FakeResolver,
    rec: Recorder,
    *,
    lan: LanTargets | None = None,
    limits: FetchLimits | None = None,
) -> GuardedFetcher:
    if lan is not None:  # an integration fetcher: the only kind that may reach a LAN target
        return IntegrationFetcher(
            resolver, lan_targets=lan, transport=rec.transport, limits=limits or FetchLimits()
        )
    return GuardedFetcher(resolver, transport=rec.transport, limits=limits or FetchLimits())


def _get(f: GuardedFetcher, url: str, **kw: object) -> object:
    return _run(f.get(url, **kw))  # type: ignore[arg-type]


@pytest.mark.req("ENF-SEC-001")
def test_connects_to_the_validated_ip_and_keeps_host_and_sni() -> None:
    rec, dns = Recorder(), FakeResolver({"files.example.org": [PUBLIC]})
    response = _get(_fetcher(dns, rec), "https://files.example.org/a/b?x=1")
    assert response.status == 200 and response.body == b"ok"  # type: ignore[attr-defined]
    (request,) = rec.requests
    assert request.url.host == PUBLIC  # the connection goes to the pinned address
    assert request.headers["host"] == "files.example.org"
    assert request.extensions["sni_hostname"] == "files.example.org"  # TLS checks the name
    assert request.url.path == "/a/b" and request.url.query == b"x=1"


@pytest.mark.req("ENF-SEC-001")
def test_host_header_keeps_a_non_default_port() -> None:
    rec, dns = Recorder(), FakeResolver({"files.example.org": [PUBLIC]})
    _get(_fetcher(dns, rec), "http://files.example.org:8080/")
    assert rec.requests[0].headers["host"] == "files.example.org:8080"
    assert rec.requests[0].url.port == 8080


@pytest.mark.req("ENF-SEC-001")
def test_dns_rebinding_is_defeated_by_a_single_resolution_per_hop() -> None:
    # the second answer would be loopback: it must never be asked for, so never be connected to
    rec = Recorder()
    dns = FakeResolver({"rebind.example.org": [[PUBLIC], ["127.0.0.1"]]})
    _get(_fetcher(dns, rec), "http://rebind.example.org/")
    assert dns.calls == [("rebind.example.org", 80)]
    assert rec.hosts == [PUBLIC]


@pytest.mark.req("ENF-SEC-001")
def test_one_forbidden_address_among_the_answers_refuses_the_host() -> None:
    rec = Recorder()
    dns = FakeResolver({"mixed.example.org": [PUBLIC, "10.0.0.7"]})
    with pytest.raises(SsrfBlocked) as caught:
        _get(_fetcher(dns, rec), "http://mixed.example.org/")
    assert caught.value.code == "ssrf.blocked"
    assert rec.requests == []


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
        "http://[fd00:ec2::254]/", "http://2130706433/", "http://0x7f.1/", "http://0177.0.0.1/",
        "http://127.1/", "http://[::ffff:127.0.0.1]/", "http://[::ffff:7f00:1]/",
        "http://[64:ff9b::7f00:1]/", "http://[2002:7f00:1::]/", "http://10.1.2.3:8080/",
        "http://100.64.0.1/", "http://0.0.0.0/", "http://255.255.255.255/", "http://224.0.0.1/",
        "http://[ff02::1]/", "http://[fe80::1]/", "http://192.168.0.1/",
    ],
)  # fmt: skip
def test_literal_destinations_are_refused_before_any_connection(url: str) -> None:
    rec = Recorder()
    with pytest.raises((SsrfBlocked, InvalidFetchUrl)):
        _get(_fetcher(FakeResolver({}), rec), url)
    assert rec.requests == []


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize("answer", ["127.0.0.1", "169.254.169.254", "::1", "::ffff:10.0.0.1"])
def test_names_resolving_to_forbidden_addresses_are_refused(answer: str) -> None:
    rec = Recorder()
    with pytest.raises(SsrfBlocked):
        _get(
            _fetcher(FakeResolver({"evil.example.org": [answer]}), rec), "http://evil.example.org/"
        )
    assert rec.requests == []


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize(
    "target",
    [
        "http://169.254.169.254/latest/",
        "http://127.0.0.1:8000/admin",
        "http://internal.example.org/",
        "http://[fd00:ec2::254]/",
    ],
)
def test_redirect_to_a_forbidden_address_is_revalidated(target: str) -> None:
    rec = Recorder(lambda r: httpx2.Response(302, headers={"location": target}))
    dns = FakeResolver({"start.example.org": [PUBLIC], "internal.example.org": ["10.0.0.9"]})
    with pytest.raises(SsrfBlocked):
        _get(_fetcher(dns, rec), "http://start.example.org/")
    assert rec.hosts == [PUBLIC]  # only the first hop was ever connected


@pytest.mark.req("ENF-SEC-001")
def test_redirects_are_followed_with_each_hop_pinned_and_relative_locations_resolved() -> None:
    def respond(r: httpx2.Request) -> httpx2.Response:
        if r.headers["host"] == "a.example.org":
            return httpx2.Response(301, headers={"location": "https://b.example.org/next"})
        if r.url.path == "/next":
            return httpx2.Response(302, headers={"location": "/final"})
        return httpx2.Response(200, content=b"done")

    rec = Recorder(respond)
    dns = FakeResolver({"a.example.org": ["93.184.216.1"], "b.example.org": ["93.184.216.2"]})
    response = _get(_fetcher(dns, rec), "http://a.example.org/")
    assert response.body == b"done"  # type: ignore[attr-defined]
    assert rec.hosts == ["93.184.216.1", "93.184.216.2", "93.184.216.2"]
    assert [c[0] for c in dns.calls] == ["a.example.org", "b.example.org", "b.example.org"]


@pytest.mark.req("ENF-SEC-001")
def test_redirect_loop_and_scheme_downgrade_to_other_schemes() -> None:
    loop = Recorder(lambda r: httpx2.Response(302, headers={"location": "/again"}))
    dns = FakeResolver({"loop.example.org": [PUBLIC]})
    with pytest.raises(TooManyRedirects):
        _get(_fetcher(dns, loop, limits=FetchLimits(max_redirects=3)), "http://loop.example.org/")
    assert len(loop.requests) == 4
    for target in ("file:///etc/passwd", "gopher://x/", "ftp://x/"):
        rec = Recorder(lambda r, t=target: httpx2.Response(302, headers={"location": t}))
        with pytest.raises((InvalidFetchUrl, SsrfBlocked)):
            _get(_fetcher(dns, rec), "http://loop.example.org/")


SECRETS = {
    "Accept": "text/html", "Authorization": "Bearer s3cret", "Cookie": "sid=1", "X-Api-Key": "k",
    "Proxy-Authorization": "Basic eA==", "X-Custom": "keep",
}  # fmt: skip


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize(
    ("location", "kept"),
    [
        ("https://other.example.org/x", False),  # another host
        ("https://a.example.org:8443/x", False),  # another port
        ("http://a.example.org/x", False),  # another scheme (downgrade)
        ("/x", True),  # same origin
        ("https://a.example.org/y", True),
    ],
)
def test_redirect_drops_auth_headers(location: str, kept: bool) -> None:
    def respond(r: httpx2.Request) -> httpx2.Response:
        if len(rec.requests) == 1:
            return httpx2.Response(302, headers={"location": location})
        return httpx2.Response(200)

    rec = Recorder(respond)
    dns = FakeResolver({"a.example.org": [PUBLIC], "other.example.org": ["93.184.216.5"]})
    _get(_fetcher(dns, rec), "https://a.example.org/", headers=dict(SECRETS))
    first, second = rec.requests
    for name in ("Authorization", "Cookie", "X-Api-Key", "Proxy-Authorization"):
        assert name.lower() in first.headers
        assert (name.lower() in second.headers) is kept
    assert ("x-custom" in second.headers) is kept  # off origin, only the allow-list travels
    assert second.headers["accept"] == "text/html"


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize(("status", "method"), [(303, "GET"), (302, "GET"), (307, "POST")])
def test_post_redirect_303_becomes_get_and_307_keeps_the_method(status: int, method: str) -> None:
    def respond(r: httpx2.Request) -> httpx2.Response:
        if len(rec.requests) == 1:
            return httpx2.Response(status, headers={"location": "/next"})
        return httpx2.Response(200)

    rec = Recorder(respond)
    dns = FakeResolver({"a.example.org": [PUBLIC]})
    _run(_fetcher(dns, rec).post("http://a.example.org/", json={"a": 1}))
    assert rec.requests[1].method == method


@pytest.mark.req("ENF-SEC-001")
def test_userinfo_and_unsupported_schemes_refused() -> None:
    f = _fetcher(FakeResolver({"a.example.org": [PUBLIC]}), Recorder())
    for url in (
        "http://user:pw@a.example.org/",
        "ftp://a.example.org/",
        "a.example.org/x",
        "http:///x",
    ):
        with pytest.raises(InvalidFetchUrl):
            _get(f, url)


@pytest.mark.req("ENF-SEC-001")
def test_no_proxy_is_used_even_when_the_environment_sets_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.invalid:3128")
    rec = Recorder()
    _get(_fetcher(FakeResolver({"a.example.org": [PUBLIC]}), rec), "http://a.example.org/")
    assert rec.hosts == [PUBLIC]


@pytest.mark.req("ENF-SEC-001")
def test_lan_target_is_reachable_only_on_its_declared_host_and_port() -> None:
    dns = FakeResolver({"nas.lan": ["192.168.1.20"]})
    lan = _run(LanTargets.declare(["nas.lan:8080", "[fd00::5]:80"], dns))
    rec = Recorder()
    f = _fetcher(dns, rec, lan=lan)
    _get(f, "http://nas.lan:8080/api")
    _get(f, "http://[fd00::5]/")
    assert rec.hosts == ["192.168.1.20", "fd00::5"]
    assert rec.requests[0].headers["host"] == "nas.lan:8080"
    for url in ("http://nas.lan:8081/", "http://nas.lan/", "http://192.168.1.21:8080/",
                "http://[fd00::6]/", "http://192.168.1.20:8080/"):  # fmt: skip
        with pytest.raises(SsrfBlocked):
            _get(f, url)
    assert len(rec.requests) == 2


@pytest.mark.req("ENF-SEC-001")
def test_lan_target_name_pinned_to_declared_addresses() -> None:
    dns = FakeResolver(
        {
            "nas.lan": [
                ["192.168.1.20", "fd00::20", "fe80::20"],
                ["fd00::20"],
                ["10.9.9.9"],
                ["fd00::20", "10.9.9.9"],
                ["fe80::20", "fd00::21"],
            ]
        }
    )
    lan = _run(LanTargets.declare(["nas.lan:80"], dns))  # call 1: the declaration
    rec = Recorder()
    f = _fetcher(dns, rec, lan=lan)
    _get(f, "http://nas.lan/")  # call 2: a subset of the declared addresses (ULA)
    assert rec.hosts == ["fd00::20"]
    with pytest.raises(SsrfBlocked):  # call 3: re-pointed to another private address
        _get(f, "http://nas.lan/")
    _get(f, "http://nas.lan/")  # call 4: only the declared address is used, the other is ignored
    assert rec.hosts == ["fd00::20", "fd00::20"]
    _get(f, "http://nas.lan/")  # call 5: one declared (link-local) among others
    assert rec.hosts[-1] == "fe80::20"


@pytest.mark.req("ENF-SEC-001")
def test_lan_target_does_not_unlock_the_metadata_service_or_other_names() -> None:
    dns = FakeResolver({"nas.lan": ["192.168.1.20"], "meta.example.org": ["169.254.169.254"]})
    lan = _run(LanTargets.declare(["nas.lan:80"], dns))
    f = _fetcher(dns, Recorder(), lan=lan)
    for url in ("http://meta.example.org/", "http://169.254.169.254/", "http://192.168.1.20/"):
        with pytest.raises(SsrfBlocked):
            _get(f, url)


@pytest.mark.req("EF-UX-003")
@pytest.mark.parametrize("sink", ["0.0.0.0", "::", "::ffff:0.0.0.0"])
def test_dns_sinkhole_reported_as_dns_blocked(sink: str) -> None:
    rec = Recorder()
    dns = FakeResolver({"blocked.example.org": [sink]})
    with pytest.raises(DnsBlocked) as caught:
        _get(_fetcher(dns, rec), "http://blocked.example.org/")
    assert caught.value.code == "source.dns_blocked"
    assert not isinstance(caught.value, SsrfBlocked)
    assert rec.requests == []


@pytest.mark.req("EF-UX-003")
def test_dns_sinkhole_with_both_families_and_nxdomain() -> None:
    dns = FakeResolver(
        {"b.example.org": ["0.0.0.0", "::"], "m.example.org": ["0.0.0.0", "10.0.0.1"]}
    )
    f = _fetcher(dns, Recorder())
    with pytest.raises(DnsBlocked):
        _get(f, "http://b.example.org/")
    with pytest.raises(
        SsrfBlocked
    ) as caught:  # a sinkhole mixed with a private address is no sinkhole
        _get(f, "http://m.example.org/")
    assert caught.value.code == "ssrf.blocked"


@pytest.mark.req("ENF-SEC-001")
def test_response_size_limit_on_declared_and_actual_length() -> None:
    dns = FakeResolver({"a.example.org": [PUBLIC]})
    big = Recorder(lambda r: httpx2.Response(200, content=b"x" * 5000))
    with pytest.raises(FetchTooLarge):
        _get(_fetcher(dns, big, limits=FetchLimits(max_bytes=1000)), "http://a.example.org/")

    async def chunks() -> AsyncIterator[bytes]:
        for _ in range(10):
            yield b"y" * 500

    lying = Recorder(lambda r: httpx2.Response(200, content=chunks()))
    with pytest.raises(FetchTooLarge):
        _get(_fetcher(dns, lying, limits=FetchLimits(max_bytes=1000)), "http://a.example.org/")
    ok = Recorder(lambda r: httpx2.Response(200, content=b"x" * 1000))
    _get(_fetcher(dns, ok, limits=FetchLimits(max_bytes=1000)), "http://a.example.org/")


@pytest.mark.req("ENF-SEC-001")
def test_decompressed_size_is_capped_not_only_the_wire_size() -> None:
    import gzip

    bomb = gzip.compress(b"\0" * 2_000_000)
    rec = Recorder(
        lambda r: httpx2.Response(200, headers={"content-encoding": "gzip"}, content=bomb)
    )
    dns = FakeResolver({"a.example.org": [PUBLIC]})
    with pytest.raises(FetchTooLarge):
        _get(_fetcher(dns, rec, limits=FetchLimits(max_bytes=100_000)), "http://a.example.org/")


@pytest.mark.req("ENF-SEC-001")
def test_total_time_limit_covers_a_stalled_response() -> None:
    async def slow(request: httpx2.Request) -> httpx2.Response:
        await asyncio.sleep(5)
        return httpx2.Response(200)

    dns = FakeResolver({"a.example.org": [PUBLIC]})
    f = GuardedFetcher(
        dns, transport=httpx2.MockTransport(slow), limits=FetchLimits(total_timeout=0.2)
    )
    with pytest.raises(FetchTimeout):
        _get(f, "http://a.example.org/")


@pytest.mark.req("ENF-SEC-001")
def test_user_agent_params_and_referer() -> None:
    rec = Recorder()
    dns = FakeResolver({"a.example.org": [PUBLIC]})
    _get(_fetcher(dns, rec), "http://a.example.org/s", params={"q": "a b"}, referer="http://r/")
    request = rec.requests[0]
    assert request.url.query == b"q=a+b" and request.headers["referer"] == "http://r/"
    assert "shelfhold" in request.headers["user-agent"].lower()


@pytest.mark.req("ENF-SEC-001")
def test_caller_cannot_override_the_host_header() -> None:
    rec = Recorder()
    dns = FakeResolver({"a.example.org": [PUBLIC]})
    _get(_fetcher(dns, rec), "http://a.example.org/", headers={"host": "169.254.169.254"})
    assert rec.requests[0].headers.get_list("host") == ["a.example.org"]
