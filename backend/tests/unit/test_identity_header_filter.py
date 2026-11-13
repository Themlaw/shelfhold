"""M3 L06 (EF-USR-002): what the outermost filter hands on, seen from the application's side."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from shelfhold.interfaces.http_api.identity_headers import (
    ForwardAuth,
    IdentityHeaderFilter,
    parse_networks,
)

SECRET = "proxy-shared-value-0123456789abcdef"  # noqa: S105 (test fixture)
TRUSTED = parse_networks(["10.0.0.0/8"])


async def _call(
    peer: str, headers: list[tuple[bytes, bytes]], *, forward: ForwardAuth | None
) -> dict[str, Any]:
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["names"] = [name.decode() for name, _ in scope["headers"]]
        seen["state"] = dict(scope.get("state", {}))

    scope = {
        "type": "http", "method": "GET", "path": "/api/v1/x", "headers": headers,
        "client": (peer, 1234),
    }  # fmt: skip
    await IdentityHeaderFilter(app, trusted=TRUSTED, forward=forward)(scope, None, None)  # type: ignore[arg-type]
    return seen


def _run(peer: str, headers: list[tuple[bytes, bytes]], *, forward: ForwardAuth | None) -> Any:
    return asyncio.run(_call(peer, headers, forward=forward))


GOOD = [
    (b"remote-user", b"alice"),
    (b"x-shelfhold-proxy-secret", SECRET.encode()),
    (b"x-forwarded-for", b"203.0.113.5"),
    (b"accept", b"*/*"),
]


@pytest.mark.req("EF-USR-002", "ENF-SEC-008")
def test_the_identity_header_never_reaches_the_application_as_a_header() -> None:
    forward = ForwardAuth(secret=SECRET)
    believed = _run("10.1.2.3", GOOD, forward=forward)
    # the name is handed on through the state, the secret and the header are gone
    assert believed["state"]["forward_user"] == "alice"
    assert believed["names"] == ["x-forwarded-for", "accept"]
    for peer, who in (("192.0.2.9", forward), ("10.1.2.3", None)):
        stranger = _run(peer, GOOD, forward=who)
        assert "forward_user" not in stranger["state"], peer
        assert stranger["names"] == ["x-forwarded-for", "accept"], peer
    wrong = [(b"remote-user", b"alice"), (b"x-shelfhold-proxy-secret", b"nope")]
    assert "forward_user" not in (_run("10.1.2.3", wrong, forward=forward))["state"]
    # the underscore spelling some servers map onto the hyphen one is stripped too
    spelled = [(b"Remote_User", b"alice"), (b"x_shelfhold_proxy_secret", SECRET.encode())]
    assert (_run("10.1.2.3", spelled, forward=forward))["names"] == []
