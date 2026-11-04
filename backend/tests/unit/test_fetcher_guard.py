"""ENF-SEC-001 / EF-UX-003: the SSRF guard (host notation and IP classification) and lan_targets."""

from __future__ import annotations

import asyncio
import ipaddress

import pytest

from shelfhold.infrastructure.fetcher import (
    DnsBlocked,
    InvalidLanTarget,
    LanTargets,
    SsrfBlocked,
    block_reason,
    parse_host,
)
from tests.fakes_fetcher import FakeResolver

FORBIDDEN = [
    "127.0.0.1", "127.255.255.254", "10.0.0.1", "172.16.0.1", "172.31.255.255", "192.168.1.1",
    "100.64.0.1", "100.127.255.255", "169.254.0.1", "169.254.169.254", "0.0.0.0", "0.1.2.3",
    "224.0.0.1", "239.255.255.255", "255.255.255.255", "240.0.0.1", "192.0.0.8", "198.18.0.1",
    "::", "::1", "fe80::1", "fd00:ec2::254", "fd12:3456::1", "fc00::1", "ff02::1", "fec0::1",
    "::ffff:127.0.0.1", "::ffff:169.254.169.254", "::ffff:7f00:1", "::127.0.0.1",
    "::ffff:0:8.8.8.8", "::ffff:0:7f00:1", "192.88.99.1", "64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b:1::1", "2002:7f00:1::", "2002:a9fe:a9fe::1",
    "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo, client 192.0.2.45 (not global)
    "2001:0:7f00:1:0:0:80ff:fefe",  # Teredo server 127.0.0.1
    "2001:0:4136:e378:8000:63bf:80ff:fffe",  # Teredo client 127.0.0.1 (obfuscated)
]  # fmt: skip
ALLOWED = ["8.8.8.8", "1.1.1.1", "93.184.216.34", "2606:4700:4700::1111", "2a00:1450:4009::200e"]


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize("ip", FORBIDDEN)
def test_ssrf_guard_refuses_forbidden_ranges(ip: str) -> None:
    assert block_reason(ipaddress.ip_address(ip)) is not None


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize("ip", ALLOWED)
def test_ssrf_guard_allows_public_addresses(ip: str) -> None:
    assert block_reason(ipaddress.ip_address(ip)) is None


@pytest.mark.req("ENF-SEC-001")
def test_nat64_and_6to4_to_a_public_address_stay_allowed() -> None:
    assert block_reason(ipaddress.ip_address("64:ff9b::808:808")) is None
    assert block_reason(ipaddress.ip_address("2002:808:808::1")) is None


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize(
    "host",
    [
        "2130706433", "0x7f000001", "0x7f.0.0.1", "0177.0.0.1", "017700000001", "127.1", "127.0.1",
        "0", "0x0", "1.1", "134744072", "08.8.8.8", "8.8.8.08", "0x8.0x8.0x8.0x8",
        "%31%32%37.0.0.1", "fe80::1%eth0", "127.0.0.1.", "example.0x7f", "example.123",
    ],
)  # fmt: skip
def test_non_canonical_ip_notations_refused(host: str) -> None:
    with pytest.raises(SsrfBlocked):
        parse_host(host)


@pytest.mark.req("ENF-SEC-001")
def test_parse_host_accepts_names_and_canonical_literals() -> None:
    assert parse_host("example.org") is None
    assert parse_host("a-1.example.org") is None
    assert parse_host("8.8.8.8") == ipaddress.ip_address("8.8.8.8")
    assert parse_host("::1") == ipaddress.ip_address("::1")


def _declare(entries: list[str], table: dict[str, list[str]] | None = None) -> LanTargets:
    return asyncio.run(LanTargets.declare(entries, FakeResolver(table or {})))


@pytest.mark.req("ENF-SEC-001")
@pytest.mark.parametrize(
    "entry",
    [
        "nas.lan", "192.168.1.10", "192.168.1.0/24:80", "*.lan:80", ":80", "nas.lan:0",
        "nas.lan:70000", "nas.lan:http", "user@nas.lan:80", "http://nas.lan:80", "nas.lan:80/x",
        "fd00::5:80", "nas.lan:80,other.lan:80", "", " nas.lan:80", "10.0.0.1:80:90", "true",
        "169.254.169.254:80", "[fd00:ec2::254]:80", "0.0.0.0:80", "[::]:80", "224.0.0.1:80",
    ],
)  # fmt: skip
def test_lan_targets_explicit_host_port_only(entry: str) -> None:
    with pytest.raises(InvalidLanTarget):
        _declare([entry], {"nas.lan": ["192.168.1.10"]})


@pytest.mark.req("ENF-SEC-001")
def test_lan_targets_is_a_list_of_strings_never_a_boolean() -> None:
    with pytest.raises(InvalidLanTarget):
        asyncio.run(LanTargets.declare(True, FakeResolver({})))  # type: ignore[arg-type]


@pytest.mark.req("ENF-SEC-001")
def test_lan_targets_declared_forms_are_accepted() -> None:
    lan = _declare(
        ["192.168.1.10:8080", "[fd00::5]:80", "[fe80::1]:443", "nas.lan:8080"],
        {"nas.lan": ["192.168.1.20", "fd00::20"]},
    )
    assert lan.addresses("nas.lan", 8080) == frozenset(
        {ipaddress.ip_address("192.168.1.20"), ipaddress.ip_address("fd00::20")}
    )
    assert lan.addresses("192.168.1.10", 8080) == frozenset({ipaddress.ip_address("192.168.1.10")})
    assert lan.addresses("fd00::5", 80)
    assert lan.addresses("fe80::1", 443)
    assert lan.addresses("192.168.1.10", 8081) is None
    assert lan.addresses("other.lan", 8080) is None


@pytest.mark.req("EF-UX-003")
def test_dns_sinkhole_declaration_reported_as_dns_blocked() -> None:
    with pytest.raises(DnsBlocked):
        _declare(["nas.lan:80"], {"nas.lan": ["0.0.0.0"]})
