"""Proxy identity headers: believed only from a configured proxy, in the one way (P5, EF-USR-002).

"Refused" means "never trusted", not "request rejected": a tunnel such as `tailscale serve` adds
`Tailscale-User-Login` to every request, and rejecting those would break the instance (P1). So the
outermost middleware **removes** every identity header from `scope["headers"]` before any other
code (present or future) can read them. Only when forward-auth is switched on
(`auth_forward_enabled`) **and** the socket peer is inside `http.trusted_proxies` **and** the
shared secret matches (constant time; when one is configured) is the name in `Remote-User`
handed on, not as a header but as `scope["state"]["forward_user"]`, for the authentication
to look up.

Never `X-Forwarded-For` here, never a header written by the caller: the peer is the socket's.

A believed proxy that sends an identity to a route with its own credentials (`/opds`, `/kosync`,
`/kobo`) is refused (403) and audited once per route: that is a mistake of configuration, not
something to guess around. `/healthz` and `/readyz` simply never take an identity.

Each header name stripped from a stranger is logged once per process and recorded in the audit,
which `shelfhold status` turns into a warning.
"""

import ipaddress
from collections.abc import Callable, Iterable
from dataclasses import dataclass

import structlog
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from shelfhold.identity.public import same_secret
from shelfhold.interfaces.http_api.catalog import error_code
from shelfhold.interfaces.http_api.problems import build_problem_response

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


def normalize_ip(value: str | None) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    """An address, or None. An IPv4-mapped IPv6 address is the IPv4 address it wraps, so that a
    network written in one form matches a peer seen in the other."""
    if not value:
        return None
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _peer_trusted(scope: Scope, trusted: tuple[IpNetwork, ...]) -> bool:
    client = scope.get("client")
    address = normalize_ip(client[0]) if client else None
    if not trusted or address is None:
        return False
    return any(address in network for network in trusted)


SECRET_HEADER = "x-shelfhold-proxy-secret"  # noqa: S105 (a header name)
USER_HEADER = "remote-user"
# Routes with a credential of their own: an identity from a proxy is a mistake there.
OWN_CREDENTIAL_PREFIXES = ("/opds", "/kosync", "/kobo")
NO_IDENTITY_PATHS = frozenset({"/healthz", "/readyz"})


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


@dataclass(frozen=True)
class ForwardAuth:
    """Forward-auth as configured. `secret` None: none is required (the network alone decides)."""

    secret: str | None = None


class IdentityHeaderFilter:
    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted: tuple[IpNetwork, ...] = (),
        forward: ForwardAuth | None = None,
        on_first_strip: Callable[[str], None] | None = None,
        on_rejected_route: Callable[[str], None] | None = None,
    ) -> None:
        self.app = app
        self.trusted = trusted
        self.forward = forward
        self.on_first_strip = on_first_strip
        self.on_rejected_route = on_rejected_route
        self._seen: set[str] = set()
        self._rejected: set[str] = set()

    def _believed(self, scope: Scope, headers: Headers) -> bool:
        if self.forward is None or not _peer_trusted(scope, self.trusted):
            return False
        wanted = self.forward.secret
        if not wanted:
            return True
        # `get`: the first of several lines; an empty or absent one never equals a real secret
        presented = headers.get(SECRET_HEADER, "")
        return bool(presented) and same_secret(presented, wanted)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        believed = self._believed(scope, headers)
        identity = headers.get(USER_HEADER, "").strip() if believed else ""
        kept: list[tuple[bytes, bytes]] = []
        stripped: set[str] = set()
        for name, value in scope["headers"]:
            if is_identity_header(name):
                stripped.add(name.decode("latin-1").lower())
            else:
                kept.append((name, value))
        if stripped:
            scope["headers"] = kept
        if believed:
            path = str(scope.get("path", ""))
            if identity and scope["type"] == "http":
                prefix = next((p for p in OWN_CREDENTIAL_PREFIXES if _under(path, p)), None)
                if prefix is not None:
                    await self._reject_route(scope, receive, send, prefix)
                    return
                if path not in NO_IDENTITY_PATHS:
                    scope.setdefault("state", {})["forward_user"] = identity
        elif stripped:
            for name in sorted(stripped - self._seen):
                self._seen.add(name)
                structlog.get_logger().warning("auth.identity_header_stripped", header=name)
                if self.on_first_strip is not None:
                    try:
                        await run_in_threadpool(self.on_first_strip, name)
                    except Exception:
                        structlog.get_logger().exception("auth.identity_header_audit_failed")
        await self.app(scope, receive, send)

    async def _reject_route(self, scope: Scope, receive: Receive, send: Send, prefix: str) -> None:
        if prefix not in self._rejected:
            self._rejected.add(prefix)
            structlog.get_logger().warning(
                "auth.forward_identity_on_own_credential_route", route=prefix
            )
            if self.on_rejected_route is not None:
                try:
                    await run_in_threadpool(self.on_rejected_route, prefix)
                except Exception:
                    structlog.get_logger().exception("auth.forward_reject_audit_failed")
        response = build_problem_response(error_code("forbidden"), request_id=None)
        await response(scope, receive, send)
