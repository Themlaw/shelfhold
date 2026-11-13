"""M3 L06 (EF-USR-002, EF-USR-024, ENF-SEC-008): forward-auth, rate limiting by real IP.

Off by default. The identity headers of a proxy are believed only from a peer inside
`http.trusted_proxies`, with the shared secret when one is configured (constant-time comparison);
in every other case they are removed and the request is anonymous: never believed, never a 401 of
their own (P5). What a forward-auth caller does is limited: no reserved action without a local
password."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from shelfhold.config import Settings
from tests.auth_env import ADMIN, PASSWORD, WRONG, AuthEnv, auth_env, db_rows

SECRET = "proxy-shared-value-0123456789"  # noqa: S105 (test fixture)
PROXY = "10.1.2.3"
SESSION = "/api/v1/auth/session"


def configured(settings: Settings, **update: Any) -> Settings:
    base: dict[str, Any] = {
        "auth_forward_enabled": True,
        "http_trusted_proxies": ("10.0.0.0/8",),
        "auth_forward_secret": SECRET,
    }
    merged = {**base, **update}
    secret = merged["auth_forward_secret"]
    merged["auth_forward_secret"] = SecretStr(secret) if isinstance(secret, str) else secret
    return settings.model_copy(update=merged)


@contextmanager
def forward_env(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    *,
    again: bool = False,  # a second instance over the same data: the admin is already there
    **update: Any,
) -> Iterator[AuthEnv]:
    with auth_env(
        monkeypatch,
        configured(settings, **update),
        with_admin=not again,
    ) as env:
        yield env


def proxied(env: AuthEnv, ip: str = PROXY, **headers: str) -> dict[str, Any]:
    """One request's worth of keyword arguments, from a client whose peer is `ip`."""
    base = {"Remote-User": ADMIN, "X-Shelfhold-Proxy-Secret": SECRET}
    return {"client": env.other_client(ip), "headers": {**base, **headers}}


def who(env: AuthEnv, ip: str = PROXY, **headers: str) -> Any:
    sent = proxied(env, ip, **headers)
    return sent["client"].get(SESSION, headers=sent["headers"])


# -- off by default -------------------------------------------------------------------------------


@pytest.mark.req("EF-USR-002", "ENF-SEC-008")
def test_identity_header_ignored_when_forward_auth_disabled(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    # default: nothing configured. Then: a proxy network is configured (for the real IP) but the
    # switch is off, and the secret is right. Either way the request is anonymous, not refused.
    for case in (settings, configured(settings, auth_forward_enabled=False)):
        with auth_env(monkeypatch, case, with_admin=case is settings) as env:
            answer = who(env)
            assert answer.status_code == 401 and answer.json()["code"] == "unauthenticated"
            assert env.client.get("/login", headers={"Remote-User": ADMIN}).status_code == 200


@pytest.mark.req("EF-USR-002")
def test_forward_auth_is_off_in_the_default_settings(settings: Settings) -> None:
    assert settings.auth_forward_enabled is False
    assert settings.http_trusted_proxies == ()
    assert settings.auth_forward_secret is None


@pytest.mark.req("EF-USR-002")
def test_forward_auth_enabled_without_a_network_is_refused_at_start(settings: Settings) -> None:
    with pytest.raises(ValueError, match="forward"):
        Settings.model_validate({**settings.model_dump(), "auth_forward_enabled": True})


# -- who is believed ------------------------------------------------------------------------------


@pytest.mark.req("EF-USR-002", "ENF-SEC-008")
def test_forward_auth_requires_trusted_cidr_and_configured_secret(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with forward_env(monkeypatch, settings) as env:
        good = who(env)
        assert good.status_code == 200, good.text
        assert good.json()["username"] == ADMIN and good.json()["credential"] == "session"
        # secret missing, empty, wrong, a prefix or an extension of the right one
        for secret in (None, "", "wrong", SECRET[:-1], SECRET + "x", SECRET.upper()):
            headers = {"X-Shelfhold-Proxy-Secret": secret} if secret is not None else {}
            sent = {"Remote-User": ADMIN, **headers}
            answer = env.other_client(PROXY).get(SESSION, headers=sent)
            assert answer.status_code == 401, secret  # anonymous, the identity header ignored
        # the user is not known here: nobody is invented from a header
        assert who(env, **{"Remote-User": "nobody"}).status_code == 401
        assert who(env, **{"Remote-User": ""}).status_code == 401
        # the secret alone, from outside the network
        assert who(env, "192.0.2.9").status_code == 401


@pytest.mark.req("EF-USR-002", "ENF-SEC-008")
def test_forward_auth_secret_is_optional_but_the_network_is_not(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    for position, update in enumerate(({"auth_forward_secret": None}, {"auth_forward_secret": ""})):
        host = {"http_trusted_proxies": (f"{PROXY}/32",)}  # no secret: one host only
        with forward_env(monkeypatch, settings, again=position > 0, **host, **update) as env:
            bare = {"Remote-User": ADMIN}
            assert env.other_client(PROXY).get(SESSION, headers=bare).status_code == 200
            assert env.other_client("192.0.2.9").get(SESSION, headers=bare).status_code == 401
            # an empty secret on both sides never counts as a match for a configured one
    with forward_env(monkeypatch, settings, again=True) as env:
        empty = {"Remote-User": ADMIN, "X-Shelfhold-Proxy-Secret": ""}
        assert env.other_client(PROXY).get(SESSION, headers=empty).status_code == 401


@pytest.mark.req("EF-USR-002", "ENF-SEC-008")
def test_forward_auth_rejected_outside_trusted_cidr(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with forward_env(monkeypatch, settings) as env:
        outsider = "192.0.2.9"
        # the address of a proxy, written by the caller: only the socket peer counts
        for forged in (PROXY, f"{PROXY}, {PROXY}", "10.0.0.1,10.1.1.1"):
            assert who(env, outsider, **{"X-Forwarded-For": forged}).status_code == 401
            assert who(env, outsider, **{"Forwarded": f"for={PROXY}"}).status_code == 401
            assert who(env, outsider, **{"X-Real-IP": PROXY}).status_code == 401
        multiple = env.other_client(outsider)
        lines = [("X-Forwarded-For", PROXY), ("X-Forwarded-For", "10.9.9.9")]
        sent = [*lines, ("Remote-User", ADMIN), ("X-Shelfhold-Proxy-Secret", SECRET)]
        assert multiple.get(SESSION, headers=sent).status_code == 401
        # IPv4-mapped IPv6: the mapped form of an outside address is outside, of an inside one
        # inside (the same address, written twice)
        assert who(env, "::ffff:192.0.2.9").status_code == 401
        assert who(env, "::ffff:10.1.2.3").status_code == 200
        # IPv6 networks are held to the same rule
    with forward_env(
        monkeypatch, settings, again=True, http_trusted_proxies=("2001:db8:1::/48",)
    ) as env:
        assert who(env, "2001:db8:1::5").status_code == 200
        assert who(env, "2001:db8:2::5").status_code == 401
        assert who(env, "10.1.2.3").status_code == 401


# -- routes that never take a forward identity ---------------------------------------------------


@pytest.mark.req("EF-USR-002", "ENF-SEC-008")
def test_remote_user_on_opds_kosync_route_rejected_and_audited(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with forward_env(monkeypatch, settings) as env:
        for path in ("/opds", "/opds/catalog", "/kosync/users/auth", "/kobo/abc/v1/initialization"):
            sent = proxied(env)
            answer = sent["client"].get(path, headers=sent["headers"])
            assert answer.status_code == 403, path
            assert answer.headers["content-type"].startswith("application/problem+json")
        rows = db_rows(
            settings, "SELECT target FROM audit WHERE action = 'auth.forward_rejected_route'"
        )
        assert {r[0] for r in rows} >= {"/opds", "/kosync", "/kobo"}
        assert ADMIN not in str(rows)  # the audit says the route, never the identity
        # not from a believed proxy: the header is simply ignored (the route answers as before)
        for ip in ("192.0.2.9",):
            sent = proxied(env, ip)
            answer = sent["client"].get("/opds/catalog", headers=sent["headers"])
            assert answer.status_code != 403
        # and a request with no identity header is not touched
        assert env.other_client(PROXY).get("/opds/catalog").status_code != 403


@pytest.mark.req("EF-USR-002")
def test_healthz_and_readyz_never_take_a_forward_identity(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with forward_env(monkeypatch, settings) as env:
        for path in ("/healthz", "/readyz"):
            sent = proxied(env)
            answer = sent["client"].get(path, headers=sent["headers"])
            assert answer.status_code in {200, 503}
            assert "username" not in answer.text and ADMIN not in answer.text


# -- reserved actions ----------------------------------------------------------------------------


NEW_PROFILE = {"username": "newcomer", "password": PASSWORD, "role": "member"}
PROFILES = "/api/v1/profiles"


def profile_names(env: AuthEnv) -> list[str]:
    return sorted(str(r[0]) for r in db_rows(env.settings, "SELECT username FROM user"))


@pytest.mark.req("EF-USR-024", "EF-USR-002")
def test_forward_auth_session_cannot_do_reserved_actions(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with forward_env(monkeypatch, settings) as env:
        before = profile_names(env)
        (admin_id,) = db_rows(env.settings, "SELECT id FROM user WHERE username = 'admin'")[0]
        calls = (
            ("POST", PROFILES, {"json": NEW_PROFILE}),  # parental control: managing profiles
            ("GET", f"{PROFILES}/{admin_id}/export", {}),  # one person's data, revealed
        )
        for method, path, kwargs in calls:
            sent = proxied(env)
            sent["headers"]["Origin"] = "https://testserver"
            answer = sent["client"].request(method, path, headers=sent["headers"], **kwargs)
            assert answer.status_code == 401, (path, answer.text)
            body = answer.json()
            assert body["code"] == "reauth_required"
            assert body["reauth_method"] in {"password", "local_password"}
        assert profile_names(env) == before  # nothing was done
        # a session opened by the local password, in the same browser, is the way in
        browser = env.other_client(PROXY)
        assert env.login(client=browser).status_code == 200
        ok = env.post(PROFILES, client=browser, json=NEW_PROFILE, headers={"Remote-User": ADMIN})
        assert ok.status_code == 201, ok.text
        assert profile_names(env) == sorted([*before, "newcomer"])


@pytest.mark.req("EF-USR-024")
def test_forward_auth_never_makes_a_session_fresh_by_itself(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with forward_env(monkeypatch, settings) as env:
        before = profile_names(env)
        # a local session whose password entry is old, plus a forward identity: still not fresh
        browser = env.other_client(PROXY)
        assert env.login(client=browser).status_code == 200
        env.clock.advance(timedelta(hours=2))
        sent = {"Remote-User": ADMIN, "X-Shelfhold-Proxy-Secret": SECRET}
        sent["X-CSRF-Token"] = env.csrf(browser)
        answer = browser.post(PROFILES, json=NEW_PROFILE, headers=sent)
        assert answer.status_code == 401 and answer.json()["code"] == "reauth_required"
        assert profile_names(env) == before


@pytest.mark.req("EF-USR-024", "ENF-SEC-008")
def test_forward_auth_unsafe_request_needs_its_own_origin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """A proxy login is an ambient credential, like a cookie: a cross-site page must not use it."""
    with forward_env(monkeypatch, settings) as env:
        path = "/api/v1/auth/logout"  # a session route that changes state and is not reserved
        sent = proxied(env)
        client, headers = sent["client"], sent["headers"]
        assert client.post(path, headers=headers).status_code == 403  # no Origin at all
        wrong = {**headers, "Origin": "https://elsewhere.example"}
        assert client.post(path, headers=wrong).status_code == 403
        cross = {**headers, "Origin": "https://testserver", "Sec-Fetch-Site": "cross-site"}
        assert client.post(path, headers=cross).status_code == 403
        right = {**headers, "Origin": "https://testserver"}
        assert client.post(path, headers=right).status_code == 204
        assert client.get(SESSION, headers=headers).status_code == 200  # reads need none


@pytest.mark.req("EF-USR-002")
def test_a_key_or_a_session_wins_over_the_forward_identity(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with forward_env(monkeypatch, settings) as env:
        sent = proxied(env, **{"Authorization": "Bearer shk_nope_nope"})
        assert sent["client"].get(SESSION, headers=sent["headers"]).status_code == 401
        browser = env.other_client(PROXY)
        assert env.login(client=browser).status_code == 200
        mixed = browser.get(SESSION, headers={"Remote-User": "nobody"})
        assert mixed.json()["username"] == ADMIN  # the session, not the header


# -- rate limiting by real IP ---------------------------------------------------------------------


def wrong_login(client: TestClient, forwarded: str | list[str] | None = None) -> Any:
    headers: list[tuple[str, str]] = []
    for value in [forwarded] if isinstance(forwarded, str) else (forwarded or []):
        headers.append(("X-Forwarded-For", value))
    return client.post(
        "/api/v1/auth/login", json={"username": ADMIN, "password": WRONG}, headers=headers
    )


@pytest.mark.req("ENF-SEC-008")
def test_login_rate_limited_real_ip_from_trusted_xff(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    update = {"auth_forward_enabled": False, "auth_forward_secret": None}
    with forward_env(monkeypatch, settings, **update) as env:
        proxy = env.other_client(PROXY)
        for _ in range(10):
            assert wrong_login(proxy, "203.0.113.5").status_code == 401
        assert wrong_login(proxy, "203.0.113.5").status_code == 429
        # another visitor behind the same proxy is not locked out by the first one
        assert wrong_login(proxy, "203.0.113.6").status_code == 401
        # a second trusted proxy in the chain; a forged left-hand entry; two header lines
        assert wrong_login(proxy, "203.0.113.5, 10.7.7.7").status_code == 429
        assert wrong_login(proxy, "1.2.3.4, 203.0.113.5").status_code == 429
        assert wrong_login(proxy, ["9.9.9.9", "203.0.113.5"]).status_code == 429
        # a garbage entry never moves the visitor to a fresh budget
        assert wrong_login(proxy, "unknown, 203.0.113.5").status_code == 429
        retry = wrong_login(proxy, "203.0.113.5")
        assert int(retry.headers["Retry-After"]) >= 1


@pytest.mark.req("ENF-SEC-008")
def test_a_client_cannot_change_address_with_a_header(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    update = {"auth_forward_enabled": False, "auth_forward_secret": None}
    with forward_env(monkeypatch, settings, **update) as env:
        direct = env.other_client("192.0.2.9")  # not a proxy: its headers are only claims
        for _ in range(10):
            assert wrong_login(direct, "198.51.100.1").status_code == 401
        for forged in ("198.51.100.2", PROXY, "::1", ["198.51.100.3", PROXY]):
            assert wrong_login(direct, forged).status_code == 429
        # IPv4-mapped IPv6 of the same address is the same address
        assert wrong_login(env.other_client("::ffff:192.0.2.9")).status_code == 429


@pytest.mark.req("ENF-SEC-008")
def test_the_whole_ipv6_prefix_shares_a_budget_and_a_mapped_address_is_its_ipv4(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        for last in range(1, 11):
            assert wrong_login(env.other_client(f"2001:db8:7:7::{last}")).status_code == 401
        assert wrong_login(env.other_client("2001:db8:7:7::ffff")).status_code == 429
        assert wrong_login(env.other_client("2001:db8:7:8::1")).status_code == 401


@pytest.mark.req("ENF-SEC-008")
def test_public_routes_are_limited_per_real_address(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    update = {"auth_forward_enabled": False, "auth_forward_secret": None,
              "http_public_rate_per_minute": 20}  # fmt: skip
    with forward_env(monkeypatch, settings, **update) as env:
        visitor = env.other_client("192.0.2.9")
        answers = [visitor.get("/login").status_code for _ in range(25)]
        assert answers[:20] == [200] * 20 and set(answers[20:]) == {429}
        limited = visitor.get("/login")
        assert limited.headers["content-type"].startswith("application/problem+json")
        assert int(limited.headers["Retry-After"]) >= 1
        # a header changes nothing for a visitor that is not a proxy
        assert (
            visitor.get("/login", headers={"X-Forwarded-For": "198.51.100.77"}).status_code == 429
        )
        # another address has its own budget; so has a visitor behind a believed proxy
        assert env.other_client("192.0.2.10").get("/login").status_code == 200
        behind = env.other_client(PROXY)
        for _ in range(20):
            assert (
                behind.get("/login", headers={"X-Forwarded-For": "203.0.113.50"}).status_code == 200
            )
        assert behind.get("/login", headers={"X-Forwarded-For": "203.0.113.50"}).status_code == 429
        assert behind.get("/login", headers={"X-Forwarded-For": "203.0.113.51"}).status_code == 200
        # health probes and a signed-in caller are not slowed down by it
        assert visitor.get("/healthz").status_code in {200, 503}


@pytest.mark.req("ENF-SEC-008")
def test_a_wrong_api_key_is_limited_per_address_and_per_key(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        guesser = env.other_client("192.0.2.30")
        headers = {"Authorization": "Bearer shk_abcdefgh_" + "0" * 32}
        codes = [guesser.get(SESSION, headers=headers).status_code for _ in range(12)]
        assert codes[:10] == [401] * 10 and 429 in codes[10:]
        # the same key from another address is limited too: the token has its own budget
        for last in range(40, 60):
            other = env.other_client(f"192.0.2.{last}")
            other.get(SESSION, headers=headers)
        assert env.other_client("192.0.2.99").get(SESSION, headers=headers).status_code == 429
        # a good password from a quiet address still works
        assert env.login(client=env.other_client("192.0.2.31")).status_code == 200
        assert PASSWORD
