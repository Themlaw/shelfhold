"""ENF-SEC-001: Hypothesis suite on the SSRF guard (classification and host notations)."""

from __future__ import annotations

import ipaddress
import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from shelfhold.infrastructure.fetcher import SsrfBlocked, block_reason, parse_host

# An independent restatement of the forbidden IPv4 space (the guard must agree with it).
V4_FORBIDDEN = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15",
    "198.51.100.0/24", "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4",
)]  # fmt: skip

v4_ints = st.integers(0, 2**32 - 1)
v4_forbidden = st.sampled_from(V4_FORBIDDEN).flatmap(
    lambda n: st.integers(int(n.network_address), int(n.broadcast_address))
)
v4_addr = v4_ints.map(ipaddress.IPv4Address)


def _is_forbidden_v4(a: ipaddress.IPv4Address) -> bool:
    return any(a in n for n in V4_FORBIDDEN)


def _embeddings(a: ipaddress.IPv4Address) -> list[ipaddress.IPv6Address]:
    n = int(a)
    hi, lo = n >> 16, n & 0xFFFF
    return [
        ipaddress.IPv6Address(f"::ffff:{hi:x}:{lo:x}"),  # mapped
        ipaddress.IPv6Address(f"64:ff9b::{hi:x}:{lo:x}"),  # NAT64
        ipaddress.IPv6Address(f"2002:{hi:x}:{lo:x}::1"),  # 6to4
        ipaddress.IPv6Address(f"2001:0:{hi:x}:{lo:x}:0:0:0:1"),  # Teredo, server
        ipaddress.IPv6Address(f"2001:0:1:2:0:0:{hi ^ 0xFFFF:x}:{lo ^ 0xFFFF:x}"),  # Teredo, client
    ]


@pytest.mark.req("ENF-SEC-001")
@given(n=v4_forbidden)
def test_ssrf_guard_property_suite(n: int) -> None:
    """Every forbidden IPv4, bare or carried by any IPv6 transition form, is refused."""
    a = ipaddress.IPv4Address(n)
    assert block_reason(a) is not None
    for wrapped in _embeddings(a):
        assert block_reason(wrapped) is not None, wrapped


@pytest.mark.req("ENF-SEC-001")
@given(a=v4_addr)
def test_ipv4_classification_matches_the_independent_oracle(a: ipaddress.IPv4Address) -> None:
    assert (block_reason(a) is not None) == _is_forbidden_v4(a)
    if not _is_forbidden_v4(a):
        # mapped, NAT64 and 6to4 forms of a public address stay public
        for wrapped in _embeddings(a)[:3]:
            assert block_reason(wrapped) is None, wrapped


@pytest.mark.req("ENF-SEC-001")
@given(raw=st.binary(min_size=16, max_size=16))
def test_ipv6_classification_never_allows_what_is_not_global(raw: bytes) -> None:
    a = ipaddress.IPv6Address(raw)
    if block_reason(a) is None:
        assert a.is_global and not a.is_multicast and not a.is_link_local and not a.is_private
    if a.is_loopback or a.is_unspecified or a.is_link_local or a.is_multicast:
        assert block_reason(a) is not None


@pytest.mark.req("ENF-SEC-001")
@given(n=v4_ints)
def test_non_canonical_notations_of_any_address_are_refused(n: int) -> None:
    a = ipaddress.IPv4Address(n)
    b = a.packed
    forms = [
        str(n),  # decimal
        hex(n),  # hexadecimal
        ".".join(f"0{x:o}" for x in b),  # octal, dotted
        ".".join(hex(x) for x in b),  # hexadecimal, dotted
        f"{b[0]}.{n & 0xFFFFFF}",  # short: a.b
        f"{b[0]}.{b[1]}.{struct.unpack('>H', b[2:])[0]}",  # short: a.b.c
        ".".join(f"{x:03d}" for x in b),  # zero padded
    ]
    for form in forms:
        try:
            canonical = str(ipaddress.IPv4Address(form)) == form
        except ValueError:
            canonical = False
        if canonical:  # e.g. "0.0.0.0" written zero padded is the same text
            continue
        with pytest.raises(SsrfBlocked):
            parse_host(form)


@pytest.mark.req("ENF-SEC-001")
@given(a=v4_addr)
def test_canonical_literals_round_trip_through_parse_host(a: ipaddress.IPv4Address) -> None:
    assert parse_host(str(a)) == a
    assert parse_host(str(ipaddress.IPv6Address(f"::ffff:{a}"))) == ipaddress.IPv6Address(
        f"::ffff:{a}"
    )


@pytest.mark.req("ENF-SEC-001")
@given(
    labels=st.lists(st.from_regex(r"[a-z][a-z0-9-]{0,10}", fullmatch=True), min_size=1, max_size=4)
)
def test_ordinary_names_are_not_literals(labels: list[str]) -> None:
    assert parse_host(".".join([*labels, "org"])) is None
