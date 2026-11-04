"""lan_targets: explicit `host:port` the admin declares, with the name pinned to its addresses.

A declaration is `host:port` (an IPv6 literal is bracketed). No wildcard, no network, no bare host
and no boolean. A name is resolved once, at declaration; afterwards the name only ever connects to
an address it had then (and each connection is re-resolved and intersected with that set), so a
changed record cannot re-point a trusted name at another machine of the network.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Sequence
from typing import Final

from shelfhold.infrastructure.fetcher.errors import (
    DnsBlocked,
    DnsResolutionError,
    InvalidLanTarget,
    SsrfBlocked,
)
from shelfhold.infrastructure.fetcher.guard import (
    IPAddress,
    is_sinkhole,
    lan_block_reason,
    parse_host,
)
from shelfhold.infrastructure.fetcher.resolver import Resolver

_V6_ENTRY: Final = re.compile(r"\[([0-9a-fA-F:.]+)\]:([0-9]{1,5})")
_NAME_ENTRY: Final = re.compile(r"([A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?):([0-9]{1,5})")


def normalize_host(host: str) -> str:
    """The key of a host: lower case, IPv6 in compressed form."""
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        return host.lower()


class LanTargets:
    def __init__(self, pinned: dict[tuple[str, int], frozenset[IPAddress]]) -> None:
        self._pinned = pinned

    @classmethod
    def empty(cls) -> LanTargets:
        return cls({})

    @classmethod
    async def declare(cls, entries: Sequence[str], resolver: Resolver) -> LanTargets:
        if not isinstance(entries, list | tuple):
            raise InvalidLanTarget("lan_targets is a list of 'host:port' strings")
        pinned: dict[tuple[str, int], frozenset[IPAddress]] = {}
        for entry in entries:
            host, port = _split(entry)
            try:
                literal = parse_host(host)
            except SsrfBlocked as exc:
                raise InvalidLanTarget(f"{entry!r}: {exc}") from exc
            if literal is not None:
                addresses = [literal]
            else:
                try:
                    answers = await resolver.resolve(host, port)
                except DnsResolutionError as exc:
                    raise InvalidLanTarget(f"{entry!r} does not resolve") from exc
                addresses = [ipaddress.ip_address(a) for a in answers]
                if not addresses:
                    raise InvalidLanTarget(f"{entry!r} does not resolve")
                if all(is_sinkhole(a) for a in addresses):
                    raise DnsBlocked(f"{host} is blocked by the DNS resolver")
            for address in addresses:
                reason = lan_block_reason(address)
                if reason is not None:
                    raise InvalidLanTarget(f"{entry!r} resolves to {address} ({reason})")
            pinned[(normalize_host(host), port)] = frozenset(addresses)
        return cls(pinned)

    def addresses(self, host: str, port: int) -> frozenset[IPAddress] | None:
        """The addresses declared for exactly this host and port, or None if undeclared."""
        return self._pinned.get((normalize_host(host), port))


def _split(entry: object) -> tuple[str, int]:
    if not isinstance(entry, str):
        raise InvalidLanTarget("lan_targets entries are 'host:port' strings")
    match = _V6_ENTRY.fullmatch(entry) or _NAME_ENTRY.fullmatch(entry)
    if match is None:
        raise InvalidLanTarget(f"{entry!r} is not an explicit host:port")
    host, port = match.group(1), int(match.group(2))
    if not 1 <= port <= 65535:
        raise InvalidLanTarget(f"{entry!r}: port out of range")
    if _V6_ENTRY.fullmatch(entry):
        try:
            ipaddress.IPv6Address(host)
        except ValueError:
            raise InvalidLanTarget(f"{entry!r}: invalid IPv6 literal") from None
    return host, port
