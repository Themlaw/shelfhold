"""Contradicteur Opus de la PR #232 (M3 L06): PoC rouges, une objection par test.

Chaque test décrit ce que la spec promet et échoue tant que le code de la PR ne le tient pas."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from shelfhold.config import Settings
from shelfhold.interfaces.http_api.identity_headers import (
    ForwardAuth,
    IdentityHeaderFilter,
    parse_networks,
)
from tests.auth_env import auth_env
from tests.federated_env import instance
from tests.integration.test_request_key_subject import FREE, bearer, issue
from tests.integration.test_requests_http import (
    REQUESTS,
    alpha,
    ports,  # noqa: F401 (autouse fixture of the request routes)
)

SESSION = "/api/v1/auth/session"


# -- B1: a `request` key passes every admin-only route that checks the role alone ----------------


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_pr232_request_key_of_an_admin_never_reaches_admin_routes(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """`admin keys create --scope request` without `--subject` acts as the first admin (owner =
    admins[0]); `may_write` is true for `request`, and `stats.require_admin` checks the role only.
    The key must open the four routes of one's own requests and nothing else."""
    with instance(monkeypatch, settings, alpha()) as api:
        key = issue(api, "admin", scope="request")
        assert api.client.post(REQUESTS, json=FREE, headers=bearer(key)).status_code == 201
        reached = {
            "GET /api/v1/stats": api.client.get("/api/v1/stats", headers=bearer(key)).status_code,
            # an admin write that is not a reserved action: 404 means the guard let the key in
            "POST /api/v1/editions/{id}/check": api.client.post(
                "/api/v1/editions/01ARZ3NDEKTSV4RRFFQ69G5FAV/check", headers=bearer(key)
            ).status_code,
        }
        assert all(code in (401, 403) for code in reached.values()), reached


# -- M1: a network that holds every address is accepted for forward-auth ------------------------


@pytest.mark.req("EF-USR-002")
@pytest.mark.parametrize("everyone", ["0.0.0.0/0", "::/0"])
def test_pr232_forward_auth_refuses_a_network_that_holds_every_address(
    settings: Settings, everyone: str
) -> None:
    """« CIDR obligatoire »: `0.0.0.0/0` or `::/0` makes any caller a believed proxy, and with no
    secret (optional) anyone on the network names herself `admin`."""
    data = settings.model_dump()
    data.update(
        auth_forward_enabled=True, http_trusted_proxies=(everyone,), auth_forward_secret=None
    )
    with pytest.raises(ValidationError):
        Settings.model_validate(data)


# -- m1: two `Remote-User` lines, the first one wins ----------------------------------------------


async def _forward_user(headers: list[tuple[bytes, bytes]]) -> Any:
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["state"] = dict(scope.get("state", {}))

    sent: list[Any] = []

    async def send(message: Any) -> None:
        sent.append(message)

    scope = {
        "type": "http", "method": "GET", "path": "/api/v1/x", "headers": headers,
        "client": ("10.1.2.3", 1234),
    }  # fmt: skip
    gate = IdentityHeaderFilter(
        app, trusted=parse_networks(["10.0.0.0/8"]), forward=ForwardAuth(secret=None)
    )
    await gate(scope, None, send)  # type: ignore[arg-type]
    return seen.get("state", {}).get("forward_user")


@pytest.mark.req("EF-USR-002")
def test_pr232_two_remote_user_lines_name_nobody() -> None:
    """A proxy that appends its line after the caller's (or forwards the caller's) leaves two
    lines: `Headers.get` hands on the first, the caller's. Ambiguity must name nobody."""
    lines = [(b"remote-user", b"admin"), (b"remote-user", b"alice")]
    assert asyncio.run(_forward_user(lines)) is None


# -- m2: wrong API keys are counted per address, not per /64 like every other budget ------------


@pytest.mark.req("ENF-SEC-008")
def test_pr232_wrong_api_keys_share_the_budget_of_the_ipv6_prefix(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """`AuthGate.by_address` is keyed by `client_ip_of()`, not `address_key()`: one /64 rotates
    its last bits and never meets the limit of wrong keys (the sign-in limit does share it)."""
    with auth_env(monkeypatch, settings) as env:
        codes = []
        for last in range(1, 13):
            guess = {"Authorization": f"Bearer shk_abcdefgh_{last:032d}"}
            codes.append(
                env.other_client(f"2001:db8:9:9::{last}").get(SESSION, headers=guess).status_code
            )
        assert 429 in codes, codes
