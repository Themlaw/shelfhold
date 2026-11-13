"""M3 L06 (ENF-SEC-008, D15): the real address of a caller, read only through trusted proxies, and
a rate table that cannot grow without bound."""

from __future__ import annotations

import pytest

from shelfhold.interfaces.http_api.identity_headers import parse_networks
from shelfhold.interfaces.http_api.ratelimit import SlidingWindowLimiter, real_client_ip

TRUSTED = parse_networks(["10.0.0.0/8", "2001:db8:aaaa::/48"])


@pytest.mark.req("ENF-SEC-008")
@pytest.mark.parametrize(
    ("peer", "forwarded", "expected"),
    [
        ("192.0.2.9", [], "192.0.2.9"),
        ("192.0.2.9", ["203.0.113.5"], "192.0.2.9"),  # the peer is not a proxy: a claim only
        ("10.1.1.1", [], "10.1.1.1"),  # a proxy that says nothing
        ("10.1.1.1", ["203.0.113.5"], "203.0.113.5"),
        ("10.1.1.1", ["1.2.3.4, 203.0.113.5"], "203.0.113.5"),  # the left is the caller's own
        ("10.1.1.1", ["203.0.113.5, 10.2.2.2"], "203.0.113.5"),  # a second proxy
        ("10.1.1.1", ["9.9.9.9", "203.0.113.5"], "203.0.113.5"),  # two header lines
        ("10.1.1.1", ["10.2.2.2"], "10.1.1.1"),  # only proxies in the chain
        ("10.1.1.1", ["garbage"], "10.1.1.1"),
        ("10.1.1.1", ["203.0.113.5, garbage"], "10.1.1.1"),  # garbage next to the proxy
        ("10.1.1.1", [""], "10.1.1.1"),
        ("10.1.1.1", ["::ffff:203.0.113.5"], "203.0.113.5"),  # mapped = the IPv4 address
        ("::ffff:10.1.1.1", ["203.0.113.5"], "203.0.113.5"),  # a mapped proxy is a proxy
        ("::ffff:192.0.2.9", ["10.1.1.1"], "192.0.2.9"),
        ("2001:db8:aaaa::1", ["2001:db8:bbbb::7"], "2001:db8:bbbb::7"),
        ("2001:db8:cccc::1", ["2001:db8:bbbb::7"], "2001:db8:cccc::1"),
        ("10.1.1.1", ["203.0.113.5:4444"], "10.1.1.1"),  # a port is not an address
        ("10.1.1.1", ["[2001:db8:bbbb::7]"], "10.1.1.1"),
    ],
)
def test_real_client_ip_only_through_trusted_proxies(
    peer: str, forwarded: list[str], expected: str
) -> None:
    assert real_client_ip(peer, forwarded, TRUSTED) == expected


@pytest.mark.req("ENF-SEC-008")
def test_real_client_ip_without_a_peer_or_a_proxy_list() -> None:
    assert real_client_ip(None, ["1.2.3.4"], TRUSTED) == "unknown"
    assert real_client_ip("10.1.1.1", ["203.0.113.5"], ()) == "10.1.1.1"  # nothing trusted
    assert real_client_ip("not an address", ["1.2.3.4"], TRUSTED) == "unknown"


@pytest.mark.req("ENF-SEC-008")
def test_rate_table_is_bounded_and_a_quiet_key_is_forgotten() -> None:
    limiter = SlidingWindowLimiter(limit=3, window_s=60.0, max_keys=100)
    for n in range(10_000):
        limiter.hit(f"ip:{n}", now=float(n) / 1000)
    assert len(limiter) <= 100
    # a burst from one key is refused, then forgiven when the window has passed
    for _ in range(3):
        assert limiter.hit("one", now=100.0) is None
    wait = limiter.hit("one", now=100.5)
    assert wait is not None and 59 <= wait <= 60
    assert limiter.hit("one", now=161.0) is None


@pytest.mark.req("ENF-SEC-008")
def test_a_flood_of_new_keys_cannot_free_a_key_that_is_being_limited_for_long() -> None:
    limiter = SlidingWindowLimiter(limit=2, window_s=60.0, max_keys=50)
    for _ in range(2):
        limiter.hit("target", now=0.0)
    assert limiter.hit("target", now=1.0) is not None
    for n in range(40):  # fewer new keys than the table holds: the target keeps its place
        limiter.hit(f"other:{n}", now=2.0)
    assert limiter.hit("target", now=3.0) is not None


@pytest.mark.req("ENF-SEC-008")
def test_a_device_route_recognises_the_credential_it_accepted() -> None:
    from types import SimpleNamespace

    from shelfhold.interfaces.http_api.ratelimit import (
        RecognisedCredentials,
        credential_label,
        device_credential,
        recognise_device_credential,
    )

    known = RecognisedCredentials(max_keys=2)
    app = SimpleNamespace(state=SimpleNamespace(recognised_devices=known))
    basic = {
        "type": "http",
        "path": "/opds/x",
        "app": app,
        "headers": [(b"authorization", b"Basic a")],
    }
    kobo = {"type": "http", "path": "/kobo/tok1/v1/x", "app": app, "headers": []}
    anonymous = {"type": "http", "path": "/opds/x", "app": app, "headers": []}
    for scope in (basic, kobo, anonymous):
        recognise_device_credential(scope)  # type: ignore[arg-type]
    assert device_credential(basic) in known and device_credential(kobo) in known  # type: ignore[arg-type]
    assert device_credential(anonymous) is None  # type: ignore[arg-type]
    assert credential_label("Basic zzz") not in known
    known.add_raw("third")  # bounded: the least recently seen goes first
    assert len(known) == 2 and device_credential(basic) not in known  # type: ignore[arg-type]
    recognise_device_credential({"type": "http", "path": "/opds", "headers": []})  # no app: no-op
