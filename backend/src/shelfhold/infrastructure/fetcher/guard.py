"""The SSRF guard: which host notations and which addresses the Fetcher will ever connect to.

Two independent checks. `parse_host` decides what a URL host *is*: an IPv4 or IPv6 literal in
canonical notation, or a DNS name. Every other way to write an address (decimal, octal, hexadecimal,
short forms, zero padding, percent-encoding, IPv6 zone) is refused, because resolvers and clients
disagree on them and the disagreement is the attack. `block_reason` decides whether an address may
be connected to; it looks through the IPv6 forms that carry an IPv4 address (mapped, compatible,
NAT64, 6to4) and refuses Teredo and every address that is not globally routable.

A plugin's Fetcher adds a third check, by name and before any resolution: `DomainAllowlist`, the
plugin's DomainRules (EF-SRC-003), at every hop.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal
from urllib.parse import urlsplit

import idna
import structlog

from shelfhold.infrastructure.fetcher.errors import DomainNotAllowed, SsrfBlocked
from shelfhold.infrastructure.logging import redact_url
from shelfhold.sdk.models import DomainRule

log = structlog.get_logger(__name__)

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
_TEREDO: Final = ipaddress.ip_network("2001::/32")
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


def embedded_v4(ip: IPAddress) -> list[ipaddress.IPv4Address]:
    """Every IPv4 address an IPv6 transition form carries: mapped, NAT64, 6to4, and Teredo (the
    server and the obfuscated client, which is the bit-wise complement of the last 4 bytes)."""
    if isinstance(ip, ipaddress.IPv4Address):
        return []
    found = []
    carried = effective_v4(ip)
    if carried is not None:
        found.append(carried)
    if ip in _TEREDO:
        packed = ip.packed
        found.append(ipaddress.IPv4Address(packed[4:8]))
        found.append(ipaddress.IPv4Address(bytes(b ^ 0xFF for b in packed[12:])))
    return found


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
    if any(v4.is_global for v4 in embedded_v4(ip)) and not isinstance(ip, ipaddress.IPv4Address):
        return "an IPv6 transition address that carries a public IPv4"
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


# -- DomainRule: the allowlist of a plugin (EF-SRC-003) --------------------------------------------
#
# `example.org` is that host only; `.example.org` the host and every name under it;
# `*.example.org` every name under it, never the host itself. A joker is the whole leftmost label
# and needs two labels after it (`*.org` would span a whole TLD; there is no public-suffix list, so
# `*.co.uk` is the admin's call). Hosts and patterns compare in their A-label form (IDNA 2008, the
# encoding the HTTP client puts on the wire), case folded, one trailing dot ignored. A pattern that
# does not parse is refused: a rule never matches more than it says.

_LDH: Final = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")


class DomainPatternError(ValueError):
    """A DomainRule pattern, a url_patterns host or a host name that is not a valid name."""


def canonical_host(name: str) -> str:
    """The comparison form of a host: an IP literal compressed, a name in lower-case A-labels
    without its trailing dot. Raises DomainPatternError for anything else."""
    if not isinstance(name, str) or not name or name != name.strip():
        raise DomainPatternError(f"{name!r} is not a host name")
    literal = name[1:-1] if name.startswith("[") and name.endswith("]") else name
    try:
        return ipaddress.ip_address(literal).compressed
    except ValueError:
        pass
    trimmed = name[:-1] if name.endswith(".") else name
    labels = trimmed.split(".")
    if not trimmed or any(not label for label in labels):
        raise DomainPatternError(f"{name!r} has an empty label")
    host = ".".join(_a_label(label, name) for label in labels)
    if len(host) > 253:
        raise DomainPatternError(f"{name!r} is longer than 253 characters")
    return host


def _a_label(label: str, name: str) -> str:
    if label.isascii():
        low = label.lower()
        if not _LDH.fullmatch(low) or (low[2:4] == "--" and not low.startswith("xn--")):
            raise DomainPatternError(f"{name!r}: {label!r} is not a host label")
        if low.startswith("xn--"):
            try:
                back = idna.encode(idna.decode(low)).decode("ascii")
            except (idna.IDNAError, UnicodeError) as exc:
                raise DomainPatternError(f"{name!r}: {label!r} is not valid punycode") from exc
            if back != low:  # pragma: no cover - idna already refuses a non-canonical A-label
                raise DomainPatternError(f"{name!r}: {label!r} is not in canonical form")
        return low
    try:
        encoded = idna.encode(label, uts46=True).decode("ascii")
    except (idna.IDNAError, UnicodeError) as exc:
        raise DomainPatternError(f"{name!r}: {label!r} is not a valid IDN label") from exc
    if "." in encoded:  # a full stop look-alike that the mapping turns into a separator
        raise DomainPatternError(f"{name!r}: {label!r} hides a label separator")
    return encoded


@dataclass(frozen=True)
class DomainPattern:
    kind: Literal["exact", "suffix", "wildcard"]
    base: str  # canonical form

    def matches(self, host: str) -> bool:
        try:
            name = canonical_host(host)
        except DomainPatternError:
            return False
        if self.kind == "exact":
            return name == self.base
        under = name.endswith("." + self.base)
        return under if self.kind == "wildcard" else (under or name == self.base)


def compile_domain_pattern(pattern: str) -> DomainPattern:
    if not isinstance(pattern, str):
        raise DomainPatternError(f"{pattern!r} is not a domain pattern")
    kind: Literal["exact", "suffix", "wildcard"] = "exact"
    rest = pattern
    if pattern.startswith("*."):
        kind, rest = "wildcard", pattern[2:]
    elif pattern.startswith("."):
        kind, rest = "suffix", pattern[1:]
    if "*" in rest:
        raise DomainPatternError(f"{pattern!r}: a joker is only the whole leftmost label")
    base = canonical_host(rest)
    if kind != "exact":
        try:
            ipaddress.ip_address(base)
        except ValueError:
            pass
        else:
            raise DomainPatternError(f"{pattern!r}: an address matches only exactly")
        labels = base.split(".")
        if len(labels) < 2 or _NUMERIC_LABEL.fullmatch(labels[-1]):
            raise DomainPatternError(f"{pattern!r} would span a whole top-level domain")
    return DomainPattern(kind, base)


_OVERRIDES: dict[tuple[str, str], str | None] = {}  # (plugin, setting) -> digest of the value
_OVERRIDES_LOCK: Final = threading.Lock()


class DomainAllowlist:
    """The domains a plugin's Fetcher may reach: its DomainRules, after the `base_url_setting`
    overrides. An override replaces the declared host by the exact host of the configured URL; it
    is logged (URL redacted) each time it changes, and a value that is not a plain http(s) URL
    makes its rule reach nothing (fail closed)."""

    def __init__(
        self,
        plugin_id: str,
        rules: Iterable[DomainRule],
        settings: Mapping[str, object] | None = None,
    ) -> None:
        self.plugin_id = plugin_id
        values = settings or {}
        entries: list[tuple[DomainPattern, str | None]] = []
        for rule in rules:
            declared = compile_domain_pattern(rule.pattern)
            if rule.base_url_setting is None:
                entries.append((declared, rule.path_prefix))
                continue
            override = self._override(rule, values.get(rule.base_url_setting))
            if override is not None:
                entries.extend((p, rule.path_prefix) for p in override)
            else:
                entries.append((declared, rule.path_prefix))
        self._entries = tuple(entries)

    def _override(self, rule: DomainRule, value: object) -> tuple[DomainPattern, ...] | None:
        """None: no override, the declared pattern stands; () refuses; else the override."""
        setting = str(rule.base_url_setting)
        key = (self.plugin_id, setting)
        if value is None or value == "":
            self._note(key, None, rule, None)
            return None
        host = _override_host(value)
        shown = redact_url(value) if isinstance(value, str) else f"<{type(value).__name__}>"
        if host is None:
            log.warning("plugin.base_url_override_refused", plugin_id=self.plugin_id,
                        setting=setting, purpose=rule.purpose, url=shown)  # fmt: skip
            return ()
        digest = hashlib.sha256(str(value).encode()).hexdigest()
        self._note(key, digest, rule, shown)
        return (DomainPattern("exact", host),)

    def _note(
        self, key: tuple[str, str], digest: str | None, rule: DomainRule, shown: str | None
    ) -> None:
        with _OVERRIDES_LOCK:
            previous = _OVERRIDES.get(key)
            changed = previous != digest and (digest is not None or key in _OVERRIDES)
            _OVERRIDES[key] = digest
        if changed:
            log.warning("plugin.base_url_override", plugin_id=self.plugin_id, setting=key[1],
                        purpose=rule.purpose, url=shown)  # fmt: skip

    def allows(self, host: str) -> bool:
        """By name only: some rule names this host (a rule limited to a path counts)."""
        return any(p.matches(host) for p, _prefix in self._entries)

    def check(self, host: str, route: str, path: str | None = None) -> None:
        """Raise DomainNotAllowed (logged with the plugin id) unless `host` is allowed, and, when
        the request path is given, unless some rule that names the host takes that path (a rule
        without a `path_prefix` takes every path)."""
        matching = [prefix for p, prefix in self._entries if p.matches(host)]
        if matching and (path is None or any(_in_prefix(path, prefix) for prefix in matching)):
            return
        log.warning("plugin.domain_not_allowed", plugin_id=self.plugin_id, route=route)
        raise DomainNotAllowed(f"{host} is not among the domains of plugin {self.plugin_id}")


_DOT_SEGMENT = re.compile(r"(?:^|/)(?:\.|\.\.)(?:/|$)")
_ESCAPES: Final = ("%2e", "%2f", "%5c", "%00")


def _in_prefix(path: str, prefix: str | None) -> bool:
    """`path` (as sent, not decoded) is under `prefix`. Whatever could move it out of the prefix
    after the server decodes it (a dot segment, an escaped dot, slash or backslash) is refused
    outright: nothing legitimate under a prefix needs one."""
    if prefix is None or path == "/robots.txt":  # politeness: the rules of the site are read
        return True
    lowered = path.lower()
    if "\\" in path or any(e in lowered for e in _ESCAPES) or _DOT_SEGMENT.search(path):
        return False
    return path.startswith(prefix)


def _override_host(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        hostname = parts.hostname
        parts.port  # noqa: B018 - raises on a malformed port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not hostname or "@" in parts.netloc:
        return None
    try:
        return canonical_host(hostname)
    except DomainPatternError:
        return None
