"""Proxy identity headers are never believed outside forward-auth (P5, review PR #41).

"Refused" means "never trusted", not "request rejected": a tunnel such as `tailscale serve` adds
`Tailscale-User-Login` to every request, and rejecting those would break the instance (P1). So the
outermost middleware **removes** them from `scope["headers"]` before any other code (present or
future) can read them, unless forward-auth is configured (`forward_auth_trusted_cidrs`) **and** the
socket peer is inside one of those networks. Forward-auth itself (reading the headers) is M3; until
then a configured CIDR only keeps the headers for it.

Each header name is logged once per process and recorded in the audit, which `shelfhold status`
turns into a warning.
"""

import ipaddress
from collections.abc import Callable, Iterable

import structlog
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Receive, Scope, Send

IpNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

IDENTITY_HEADERS = frozenset(
    {
        "remote-user",
        "remote-email",
        "remote-groups",
        "remote-name",
        "x-remote-user",
        "x-forwarded-user",
        "x-forwarded-email",
        "x-forwarded-groups",
        "x-forwarded-preferred-username",
        "x-auth-request-user",
        "x-auth-request-email",
        "x-auth-request-groups",
        "x-auth-request-preferred-username",
        "x-webauth-user",
        "x-shelfhold-proxy-secret",
    }
)
IDENTITY_HEADER_PREFIXES = ("tailscale-user-", "remote-")


def is_identity_header(raw_name: bytes) -> bool:
    # `_` is folded to `-`: some servers map `Remote_User` onto `Remote-User` (and back).
    name = raw_name.decode("latin-1").lower().replace("_", "-")
    return name in IDENTITY_HEADERS or name.startswith(IDENTITY_HEADER_PREFIXES)


def parse_networks(cidrs: Iterable[str]) -> tuple[IpNetwork, ...]:
    return tuple(ipaddress.ip_network(cidr, strict=False) for cidr in cidrs)


def _peer_trusted(scope: Scope, trusted: tuple[IpNetwork, ...]) -> bool:
    client = scope.get("client")
    if not trusted or not client:
        return False
    try:
        address = ipaddress.ip_address(client[0])
    except ValueError:
        return False
    return any(address in network for network in trusted)


class IdentityHeaderFilter:
    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted: tuple[IpNetwork, ...] = (),
        on_first_strip: Callable[[str], None] | None = None,
    ) -> None:
        self.app = app
        self.trusted = trusted
        self.on_first_strip = on_first_strip
        self._seen: set[str] = set()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] in {"http", "websocket"} and not _peer_trusted(scope, self.trusted):
            kept: list[tuple[bytes, bytes]] = []
            stripped: set[str] = set()
            for name, value in scope["headers"]:
                if is_identity_header(name):
                    stripped.add(name.decode("latin-1").lower())
                else:
                    kept.append((name, value))
            if stripped:
                scope["headers"] = kept
                for name in sorted(stripped - self._seen):
                    self._seen.add(name)
                    structlog.get_logger().warning("auth.identity_header_stripped", header=name)
                    if self.on_first_strip is not None:
                        try:
                            await run_in_threadpool(self.on_first_strip, name)
                        except Exception:
                            structlog.get_logger().exception("auth.identity_header_audit_failed")
        await self.app(scope, receive, send)
