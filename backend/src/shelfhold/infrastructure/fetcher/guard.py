"""The SSRF guard: which host notations and which addresses the Fetcher will ever connect to.

Two independent checks. `parse_host` decides what a URL host *is*: an IPv4 or IPv6 literal in
canonical notation, or a DNS name. Every other way to write an address (decimal, octal, hexadecimal,
short forms, zero padding, percent-encoding, IPv6 zone) is refused, because resolvers and clients
disagree on them and the disagreement is the attack. `block_reason` decides whether an address may
be connected to; it looks through the IPv6 forms that carry an IPv4 address (mapped, compatible,
NAT64, 6to4) and refuses Teredo and every address that is not globally routable.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Final

from shelfhold.infrastructure.fetcher.errors import SsrfBlocked

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

_V4_BLOCKED: Final = [
    (ipaddress.ip_network(n), why)
    for n, why in (
        ("0.0.0.0/8", "this network"),
        ("10.0.0.0/8", "private"),
        ("100.64.0.0/10", "carrier-grade NAT"),
        ("127.0.0.0/8", "loopback"),
        ("169.254.0.0/16", "link-local and cloud metadata"),
        ("172.16.0.0/12", "private"),
        ("192.0.0.0/24", "IETF protocol assignments"),
        ("192.0.2.0/24", "documentation"),
        ("192.168.0.0/16", "private"),
        ("192.88.99.0/24", "6to4 relay anycast"),
        ("198.18.0.0/15", "benchmarking"),
        ("198.51.100.0/24", "documentation"),
        ("203.0.113.0/24", "documentation"),
        ("224.0.0.0/4", "multicast"),
        ("240.0.0.0/4", "reserved and broadcast"),
    )
]
_V6_BLOCKED: Final = [
    (ipaddress.ip_network(n), why)
    for n, why in (
        ("::/96", "unspecified, loopback and IPv4-compatible"),
        ("100::/64", "discard-only"),
        ("::ffff:0:0:0/96", "SIIT translated"),
        ("2001::/32", "Teredo"),
        ("2001:db8::/32", "documentation"),
        ("64:ff9b:1::/48", "local-use NAT64"),
        ("fc00::/7", "unique local (includes the cloud metadata address)"),
        ("fe80::/10", "link-local"),
        ("fec0::/10", "site-local"),
        ("ff00::/8", "multicast"),
    )
]
_NAT64: Final = ipaddress.ip_network("64:ff9b::/96")
_SIXTOFOUR: Final = ipaddress.ip_network("2002::/16")
_METADATA: Final = (ipaddress.ip_address("169.254.169.254"), ipaddress.ip_address("fd00:ec2::254"))
_NUMERIC_LABEL: Final = re.compile(r"0x[0-9a-f]*|[0-9]+", re.IGNORECASE)
_V4_UNSPECIFIED: Final = ipaddress.IPv4Address(0)


def parse_host(host: str) -> IPAddress | None:
    """The address a URL host spells, or None for a DNS name. Refuses non-canonical notations."""
    if not host or "%" in host or not host.isascii():
        raise SsrfBlocked(f"host {host!r} uses an unsupported notation")
    if ":" in host:
        try:
            return ipaddress.IPv6Address(host)
        except ValueError:
            raise SsrfBlocked(f"host {host!r} is not a valid IPv6 literal") from None
    last = host.rsplit(".", 1)[-1]
    if last == "" or _NUMERIC_LABEL.fullmatch(last):
        # a trailing dot or a numeric last label: a client may read the whole host as an address
        try:
            literal = ipaddress.IPv4Address(host)
        except ValueError:
            raise SsrfBlocked(f"host {host!r} is a non-canonical address notation") from None
        if str(literal) != host:  # pragma: no cover - IPv4Address is strict
            raise SsrfBlocked(f"host {host!r} is a non-canonical address notation")
        return literal
    return None


def effective_v4(ip: IPAddress) -> ipaddress.IPv4Address | None:
    """The IPv4 address an IPv6 transition form carries (mapped, NAT64, 6to4), if any."""
    if isinstance(ip, ipaddress.IPv4Address):
        return ip
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    packed = ip.packed
    if ip in _NAT64:
        return ipaddress.IPv4Address(packed[12:])
    if ip in _SIXTOFOUR:
        return ipaddress.IPv4Address(packed[2:6])
    return None


def block_reason(ip: IPAddress) -> str | None:
    """Why a public destination may not be this address, or None when it may."""
    v4 = effective_v4(ip)
    if v4 is not None:
        for net, why in _V4_BLOCKED:
            if v4 in net:
                return why if v4 is ip else f"{why} (carried by {ip})"
        return None if v4.is_global else "not globally routable"
    for net6, why in _V6_BLOCKED:
        if ip in net6:
            return why
    return None if ip.is_global else "not globally routable"


def is_sinkhole(ip: IPAddress) -> bool:
    """0.0.0.0 or :: (also mapped): what a DNS block list answers for a blocked domain."""
    return ip.is_unspecified or effective_v4(ip) == _V4_UNSPECIFIED


def lan_block_reason(ip: IPAddress) -> str | None:
    """What stays forbidden even for a declared LAN target: it is the admin's own network, so
    private, loopback, CGNAT, link-local and ULA are allowed, but never the cloud metadata
    service, multicast, broadcast, the unspecified address or the reserved ranges."""
    v4 = effective_v4(ip)
    if v4 is not None:
        if v4 in _METADATA or ip in _METADATA:
            return "cloud metadata service"
        if v4 in ipaddress.ip_network("0.0.0.0/8") or v4.is_multicast or v4.is_reserved:
            return "unspecified, multicast, broadcast or reserved"
        return None
    if ip in _METADATA:
        return "cloud metadata service"
    if ip.is_unspecified or ip.is_multicast:
        return "unspecified or multicast"
    return None
