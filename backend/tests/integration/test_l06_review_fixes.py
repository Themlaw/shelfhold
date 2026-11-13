"""M3 L06, answers to the contradicteur of the #232: the PoCs of `test_review_pr232.py` stay as
they are; what they do not say, and what was decided at review, is attacked here."""

from __future__ import annotations

import asyncio
import re
import sqlite3
from typing import Any

import pytest
from pydantic import SecretStr, ValidationError

from shelfhold.config import Settings
from shelfhold.interfaces.http_api.identity_headers import (
    ForwardAuth,
    IdentityHeaderFilter,
    parse_networks,
)
from tests.auth_env import ADMIN, AuthEnv, auth_env, db_rows, run_cli
from tests.federated_env import instance
from tests.integration.test_child_surfaces import _routes
from tests.integration.test_forward_auth import PROXY, SECRET, SESSION, configured, who
from tests.integration.test_request_key_subject import FREE, bearer, issue
from tests.integration.test_requests_http import REQUESTS, alpha, ports  # noqa: F401

ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"
OWN_REQUESTS = {  # the four routes of one's own requests, and nothing else
    ("GET", "/api/v1/requests"),
    ("POST", "/api/v1/requests"),
    ("GET", "/api/v1/requests/{request_id}"),
    ("POST", "/api/v1/requests/{request_id}/cancel"),
}


def routes_of(env: AuthEnv) -> list[tuple[str, str]]:
    found = []
    for entry in sorted(_routes(env.app)):
        method, path = entry.split(" ", 1)
        if method == "MOUNT":  # the page of the application: a GET like any other
            found.append(("GET", path.rstrip("/") + "/some-page"))
        elif method != "WEBSOCKET":  # a WebSocket never carries a key (the middleware is HTTP)
            found.append((method, path))
    return found


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_a_request_key_reaches_the_four_own_request_routes_and_nothing_else(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """Architecture: every route of the application, called with a key of scope `request`, is
    refused (403 `credential_not_allowed`) unless it is one of the four. The refusal is the
    middleware's, before any guard, so that a guard that checks the role alone cannot let it in."""
    with instance(monkeypatch, settings, alpha()) as api:
        for subject in (None, "member", "kid"):
            key = issue(api, "admin", scope="request", subject=subject)
            seen = routes_of(api)
            assert len(seen) > 100 and set(seen) >= OWN_REQUESTS, len(seen)
            for method, template in seen:
                path = re.sub(r"\{[^}]+\}", ULID, template)
                answer = api.client.request(method, path, headers=bearer(key))
                refused = (
                    answer.status_code == 403
                    and answer.headers["content-type"].startswith("application/problem+json")
                    and answer.json()["code"] == "credential_not_allowed"
                )
                if (method, template) in OWN_REQUESTS:
                    assert not refused, (subject, method, template)
                else:
                    assert refused, (subject, method, template, answer.status_code)
        # `queue` is a parents' route that looks like an `{id}`
        key = issue(api, "admin", scope="request")
        assert api.client.get(f"{REQUESTS}/queue", headers=bearer(key)).status_code == 403
        assert api.client.get(f"{REQUESTS}/{ULID}/approval", headers=bearer(key)).status_code == 403
        assert api.client.post(REQUESTS, json=FREE, headers=bearer(key)).status_code == 201


@pytest.mark.req("EF-USR-024")
def test_cli_refuses_a_request_key_without_a_subject(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        capsys.readouterr()
        assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--scope",
                       "request") == 1  # fmt: skip
        assert "--subject" in capsys.readouterr().err
        assert db_rows(api.settings, "SELECT count(*) FROM api_key") == [(0,)]
        # read and admin keys keep their default holder
        assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--read-only") == 0


# -- the network of the proxy ---------------------------------------------------------------------


def validated(settings: Settings, **update: Any) -> Settings:
    data = settings.model_dump()
    data.update(update)
    return Settings.model_validate(data)


@pytest.mark.req("EF-USR-002")
def test_forward_auth_on_a_network_needs_the_secret_and_a_single_host_does_not(
    settings: Settings,
) -> None:
    on = {"auth_forward_enabled": True}
    for secret in (None, SecretStr("")):
        for wide in ("10.0.0.0/8", "10.1.2.0/24", "10.1.2.3/31", "2001:db8::/32"):
            with pytest.raises(ValidationError):
                validated(settings, **on, http_trusted_proxies=(wide,), auth_forward_secret=secret)
        # one wide network among hosts is still a wide network
        with pytest.raises(ValidationError):
            validated(settings, **on, http_trusted_proxies=("10.1.2.3/32", "10.9.0.0/16"),
                      auth_forward_secret=secret)  # fmt: skip
        for host in ("10.1.2.3/32", "10.1.2.3", "2001:db8::5/128"):
            assert validated(settings, **on, http_trusted_proxies=(host,),
                             auth_forward_secret=secret).auth_forward_enabled  # fmt: skip
    wide_with_secret = validated(
        settings,
        **on,
        http_trusted_proxies=("10.0.0.0/8",),
        auth_forward_secret=SecretStr("x" * 32),
    )
    assert wide_with_secret.auth_forward_enabled
    # a network that is only read for X-Forwarded-For needs no secret, but never `/0`
    assert validated(settings, http_trusted_proxies=("10.0.0.0/8",)).http_trusted_proxies
    for everyone in ("0.0.0.0/0", "::/0"):
        with pytest.raises(ValidationError):
            validated(settings, http_trusted_proxies=(everyone,))


@pytest.mark.req("EF-USR-002", "ENF-SEC-008")
def test_a_single_host_proxy_without_secret_is_believed_only_from_that_host(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    host = settings.model_copy(
        update={"auth_forward_enabled": True, "http_trusted_proxies": ("10.1.2.3/32",)}
    )
    with auth_env(monkeypatch, host) as env:
        bare = {"Remote-User": ADMIN}
        assert env.other_client(PROXY).get(SESSION, headers=bare).status_code == 200
        assert env.other_client("10.1.2.4").get(SESSION, headers=bare).status_code == 401


# -- several lines are an ambiguity ---------------------------------------------------------------


async def _state(headers: list[tuple[bytes, bytes]], secret: str | None) -> Any:
    seen: dict[str, Any] = {}

    async def app(scope: Any, receive: Any, send: Any) -> None:
        seen["state"] = dict(scope.get("state", {}))

    async def send(message: Any) -> None:
        return None

    scope = {
        "type": "http", "method": "GET", "path": "/api/v1/x", "headers": headers,
        "client": ("10.1.2.3", 1234),
    }  # fmt: skip
    gate = IdentityHeaderFilter(
        app, trusted=parse_networks(["10.0.0.0/8"]), forward=ForwardAuth(secret=secret)
    )
    await gate(scope, None, send)  # type: ignore[arg-type]
    return seen.get("state", {}).get("forward_user")


@pytest.mark.req("EF-USR-002")
@pytest.mark.parametrize(
    "lines",
    [
        [(b"remote-user", b"alice"), (b"remote-user", b"alice")],  # even twice the same name
        [(b"remote-user", b"admin, alice")],  # a folded pair
        [(b"remote-user", b"alice,")],
        [(b"remote-user", b"")],
        [(b"remote-user", b"   ")],
    ],
)
def test_ambiguous_or_empty_identity_names_nobody(lines: list[tuple[bytes, bytes]]) -> None:
    assert asyncio.run(_state(lines, None)) is None
    assert asyncio.run(_state([(b"remote-user", b"alice")], None)) == "alice"


@pytest.mark.req("EF-USR-002")
def test_two_secret_lines_are_never_believed() -> None:
    good = SECRET.encode()
    one = [(b"remote-user", b"alice"), (b"x-shelfhold-proxy-secret", good)]
    assert asyncio.run(_state(one, SECRET)) == "alice"
    for secrets in ([good, b"nope"], [b"nope", good], [good, good]):
        lines = [(b"remote-user", b"alice")] + [(b"x-shelfhold-proxy-secret", v) for v in secrets]
        assert asyncio.run(_state(lines, SECRET)) is None, secrets


# -- the name is compared without case, as the usernames are unique without case ------------------


@pytest.mark.req("EF-USR-002")
def test_forward_name_ignores_case_because_usernames_are_unique_without_case(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, configured(settings)) as env:
        for spelled in ("ADMIN", "Admin", "admin"):
            answer = who(env, **{"Remote-User": spelled})
            assert answer.status_code == 200 and answer.json()["username"] == ADMIN
        # the rule holds only because two names that differ by case cannot both exist
        connection = sqlite3.connect(env.settings.database_path)
        try:
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "INSERT INTO user (id, username, role) VALUES ('01J0000000000000000000DUPE', "
                    "'ADMIN', 'member')"
                )
        finally:
            connection.close()


# -- the routes with their own credentials have their own limit -----------------------------------


@pytest.mark.req("ENF-SEC-008")
def test_device_routes_give_a_budget_of_its_own_only_to_an_accepted_credential(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    update = {"http_public_rate_per_minute": 10, "http_device_rate_per_minute": 30}
    with auth_env(monkeypatch, settings.model_copy(update=update)) as env:
        known = env.app.state.recognised_devices  # type: ignore[attr-defined]
        basic = {"Authorization": "Basic Zm9vOmJhcg=="}
        known.add_raw(basic["Authorization"])  # what a device route does once it accepted it
        device = env.other_client("192.0.2.40")
        # 25 requests of an accepted device: far above the anonymous budget of 10
        codes = [device.get("/opds/catalog", headers=basic).status_code for _ in range(25)]
        assert 429 not in codes, codes
        # refused only at its own limit (30)
        more = [device.get("/opds/catalog", headers=basic).status_code for _ in range(10)]
        assert more[-1] == 429 and 429 not in more[:5], more
        # an unaccepted credential is an anonymous request of the address: budget of 10
        guess = {"Authorization": "Basic YmFyOmZvbw=="}
        other = env.other_client("192.0.2.43")
        tried = [other.get("/opds/catalog", headers=guess).status_code for _ in range(12)]
        assert tried[9] != 429 and tried[-1] == 429, tried
        # a Kobo token travels in the path: accepted once, it counts for itself
        known.add_raw("tokenone")
        kobo = env.other_client("192.0.2.44")
        assert all(kobo.get("/kobo/tokenone/v1/initialization").status_code != 429
                   for _ in range(15))  # fmt: skip
        # the accepted devices did not use the anonymous budget of their address
        assert all(device.get("/login").status_code == 200 for _ in range(9))


@pytest.mark.req("ENF-SEC-008")
def test_accepted_credentials_of_one_address_have_a_ceiling_ten_times_higher(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    update = {"http_public_rate_per_minute": 10, "http_device_rate_per_minute": 10_000}
    with auth_env(monkeypatch, settings.model_copy(update=update)) as env:
        known = env.app.state.recognised_devices  # type: ignore[attr-defined]
        device = env.other_client("192.0.2.50")
        codes = []
        for n in range(105):  # a hundred devices behind one address, each accepted
            known.add_raw(f"Basic dev{n}")
            codes.append(
                device.get("/opds/c", headers={"Authorization": f"Basic dev{n}"}).status_code
            )
        assert 429 not in codes[:100] and codes[-1] == 429, codes[95:]


@pytest.mark.req("EF-USR-024")
def test_the_service_refuses_a_request_key_without_a_subject(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from shelfhold.identity.public import local_auth
    from shelfhold.infrastructure.db import create_db_engine

    with instance(monkeypatch, settings, alpha()) as api:
        engine = create_db_engine(api.settings.database_path)
        try:
            auth = local_auth(engine, api.settings)
            with pytest.raises(ValueError, match="subject_required"):
                auth.create_api_key(name="k", username="admin", scope="request")
            assert auth.create_api_key(
                name="k", username="admin", scope="request", subject_username="admin"
            ).value
        finally:
            engine.dispose()
        assert db_rows(api.settings, "SELECT count(*) FROM api_key") == [(1,)]


@pytest.mark.req("EF-USR-002")
def test_the_forward_auth_secret_holds_at_least_32_characters(settings: Settings) -> None:
    on = {"auth_forward_enabled": True, "http_trusted_proxies": ("10.1.2.3/32",)}
    for short in ("x", "x" * 31, "proxy-shared-value-0123456789"):
        with pytest.raises(ValidationError, match="32"):
            validated(settings, **on, auth_forward_secret=SecretStr(short))
    assert validated(settings, **on, auth_forward_secret=SecretStr("x" * 32)).auth_forward_enabled
    assert validated(settings, **on, auth_forward_secret=SecretStr("")).auth_forward_enabled
