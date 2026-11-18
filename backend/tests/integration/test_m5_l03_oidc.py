"""M5 L03: OpenID Connect sign-in (EF-USR-004, EF-USR-024, ENF-SEC-008, ENF-PRIV-002, ADR-0018).

The authorization code flow with PKCE, `state` and `nonce`; the ID token verified (signature by
the provider's keys, `iss`, `aud`, `exp`, `nonce`); an account linked by `(issuer, sub)` to an
account that already exists; no account ever created by a sign-in; a child profile never linked.
The provider is the in-process one (`tests/fakes/oidc`): keys drawn at the test, no network.
Everything goes through the real application, the discovery and the keys through the guarded
Fetcher.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from shelfhold.composition import Network
from shelfhold.config import Settings, load_settings
from tests.auth_env import (
    CSRF_HEADER,
    SESSION_COOKIE,
    AuthEnv,
    auth_env,
    db_dump,
    db_rows,
)
from tests.fakes.oidc.provider import CLIENT_ID, ISSUER, PUBLIC_IP, FakeOidcProvider
from tests.fakes_fetcher import FakeResolver
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-USR-004")

OIDC = "/api/v1/auth/oidc"
LOGIN = f"{OIDC}/login"
CALLBACK = f"{OIDC}/callback"
LINK = f"{OIDC}/link"
SESSION = "/api/v1/auth/session"
FLOW_COOKIE = "__Host-oidcflow"


def configured(settings: Settings, provider: FakeOidcProvider, **update: Any) -> Settings:
    values: dict[str, Any] = {
        "public_url": "https://testserver",
        "oidc_issuer": provider.issuer,
        "oidc_client_id": provider.client_id,
        "oidc_client_secret": SecretStr(provider.client_secret),
        **update,
    }
    return settings.model_copy(update=values)


class World:
    def __init__(self, env: AuthEnv, provider: FakeOidcProvider) -> None:
        self.env = env
        self.idp = provider
        self.admin = env.client
        assert env.login().status_code == 200
        self.mum = add_profile(env, "mum", "member", "10.0.0.2")
        self.dad = add_profile(env, "dad", "member", "10.0.0.3")
        self.kid = add_profile(env, "kid", "child", "10.0.0.4", max_age_rating=10)
        self.ids = {
            name: str(db_rows(env.settings, "SELECT id FROM user WHERE username=?", name)[0][0])
            for name in ("admin", "mum", "dad", "kid")
        }

    def browser(self, ip: str = "198.51.100.7") -> TestClient:
        return self.env.other_client(ip)

    def send(self, client: TestClient, method: str, path: str, body: Any = None) -> Any:
        headers = {CSRF_HEADER: self.env.csrf(client)}
        return client.request(method, path, headers=headers, json=body)

    # --- the sign-in, step by step -------------------------------------------------------------

    def begin(self, client: TestClient) -> str:
        """The browser asks Shelfhold to sign in: where it is sent."""
        answer = client.get(LOGIN, follow_redirects=False)
        assert answer.status_code == 302, answer.text
        return str(answer.headers["location"])

    def callback(self, client: TestClient, url: str, **extra: Any) -> Any:
        """The provider sent the browser back: the path and query of the address."""
        parts = urlsplit(url)
        return client.get(f"{parts.path}?{parts.query}", follow_redirects=False, **extra)

    def sign_in(self, client: TestClient, *, sub: str | None = None) -> Any:
        return self.callback(client, self.idp.authorize(self.begin(client), sub=sub))

    def link_admin(self, username: str, sub: str | None = None) -> Any:
        return self.send(
            self.admin,
            "PUT",
            f"/api/v1/profiles/{self.ids[username]}/oidc",
            {"sub": sub or self.idp.sub},
        )

    def identities(self) -> list[tuple[Any, ...]]:
        return db_rows(
            self.env.settings, "SELECT user_id, issuer, sub FROM oidc_identity ORDER BY linked_at"
        )

    def actions(self) -> list[str]:
        return [str(a) for (a,) in db_rows(self.env.settings, "SELECT action FROM audit")]

    def signed_in_as(self, client: TestClient) -> str | None:
        answer = client.get(SESSION)
        return str(answer.json()["username"]) if answer.status_code == 200 else None


@contextmanager
def world_for(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    *,
    provider: FakeOidcProvider | None = None,
    network: Network | None = None,
    **update: Any,
) -> Iterator[World]:
    provider = provider or FakeOidcProvider()
    network = network or Network(resolver=provider.resolver, transport=provider.transport)
    with auth_env(monkeypatch, configured(settings, provider, **update), network=network) as env:
        provider.now = lambda: env.clock.now().timestamp()  # the tokens carry the test's time
        yield World(env, provider)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with world_for(monkeypatch, settings) as w:
        yield w


def refused(answer: Any) -> bool:
    """A refused sign-in sends the browser back to the login page, with no session."""
    return (
        answer.status_code == 303
        and answer.headers["location"].startswith("/login")
        and SESSION_COOKIE not in "".join(answer.headers.get_list("set-cookie"))
    )


# --- the sign-in ------------------------------------------------------------------------------


def test_oidc_login(world: World) -> None:
    assert world.link_admin("mum").status_code == 204
    browser = world.browser()
    url = world.begin(browser)
    assert url.startswith(f"{ISSUER}/authorize?")  # the provider's own endpoint, from discovery
    assert FLOW_COOKIE in browser.cookies  # the browser that asked is the browser that finishes
    flow_cookie = next(
        c for c in browser.get(LOGIN, follow_redirects=False).headers.get_list("set-cookie")
    )
    assert "HttpOnly" in flow_cookie and "Secure" in flow_cookie
    assert "SameSite=Lax" in flow_cookie  # a Strict cookie is not sent back by the provider

    answer = world.callback(browser, world.idp.authorize(world.begin(browser)))
    assert answer.status_code == 303 and answer.headers["location"] == "/"
    assert world.signed_in_as(browser) == "mum"
    info = browser.get(SESSION).json()
    assert info["credential"] == "session"
    assert "auth.oidc_login" in world.actions()
    assert FLOW_COOKIE not in browser.cookies or browser.cookies[FLOW_COOKIE] == ""  # used up
    # the provider saw the code and the verifier, and the keys were read: nothing else
    token_call = next(r for r in world.idp.requests if r.url.path == "/token")
    assert b"code_verifier=" in token_call.content
    assert world.idp.paths.count("/token") == 1 and "/jwks" in world.idp.paths
    assert "authorization" in token_call.headers  # a confidential client proves itself


@pytest.mark.parametrize("age", [None, 0, 30], ids=["no-claim", "just-now", "half-a-minute"])
def test_oidc_session_is_never_fresh_whatever_the_provider_says(
    world: World, age: int | None
) -> None:
    """No local secret was typed and nothing asked the provider to sign the person in again: a
    recent `auth_time` is not believed (L04 asks with `prompt=login` and `max_age` first). Else a
    compromised account at the provider would open every action that asks for a fresh session."""
    world.link_admin("mum")
    world.idp.auth_time_age_s = age
    browser = world.browser()
    assert world.sign_in(browser).status_code == 303
    refused_link = world.send(browser, "POST", LINK)
    assert refused_link.status_code == 401 and refused_link.json()["code"] == "reauth_required"


def test_oidc_login_renews_the_session_identifier(world: World) -> None:
    world.link_admin("mum")
    browser = world.browser()
    assert world.env.login("dad", client=browser).status_code == 200  # a session already held
    before = browser.cookies[SESSION_COOKIE]
    assert world.sign_in(browser).status_code == 303
    assert browser.cookies[SESSION_COOKIE] != before  # never adopted
    old = world.browser("198.51.100.8")
    old.cookies.set(SESSION_COOKIE, before)
    assert old.get(SESSION).status_code == 401  # and the old one is closed


def test_oidc_unknown_sub_creates_no_account(world: World) -> None:
    users_before = db_rows(world.env.settings, "SELECT username FROM user ORDER BY username")
    browser = world.browser()
    # a provider-side person with the same name as an account is not that account
    world.idp.tamper = {"preferred_username": "admin", "email": "admin@example.test"}
    answer = world.sign_in(browser, sub="somebody-else")
    assert refused(answer)
    assert (
        db_rows(world.env.settings, "SELECT username FROM user ORDER BY username") == users_before
    )
    assert world.identities() == []
    assert world.signed_in_as(browser) is None
    assert "somebody-else" not in db_dump(world.env.settings)  # not even kept
    # the answer does not say whether the sub is known: a linked one that fails says the same
    world.link_admin("mum")
    world.idp.tamper = {"aud": "another-client"}
    wrong = world.sign_in(world.browser("198.51.100.9"))
    assert wrong.headers["location"] == answer.headers["location"]


@pytest.mark.parametrize(
    "tamper",
    [
        {"aud": "another-client"},
        {"aud": ["another-client", "third"]},
        {"iss": "https://elsewhere.example.test"},
        {"nonce": "not-the-one-sent"},
        {"nonce": None},
        {"exp": 1},
        {"sub": None},
        {"sub": ""},
        {"iat": 4102444800},  # the year 2100
        {"exp": None},
        {"__alg": "HS256"},  # signed with the client's own secret
    ],
    ids=[
        "aud",
        "aud-list",
        "iss",
        "nonce",
        "no-nonce",
        "expired",
        "no-sub",
        "empty-sub",
        "iat-future",
        "no-exp",
        "shared-secret-signature",
    ],
)
def test_oidc_state_nonce_and_audience_checked(world: World, tamper: dict[str, Any]) -> None:
    world.link_admin("mum")
    world.idp.publish_shared_secret = tamper.get("__alg") == "HS256"
    world.idp.tamper = tamper
    browser = world.browser()
    assert refused(world.sign_in(browser))
    assert world.signed_in_as(browser) is None
    # the same claims, untouched, sign in: the refusal was the claim, not the setup
    world.idp.tamper = {}
    assert world.sign_in(world.browser("198.51.100.20")).status_code == 303


def test_oidc_token_signature_and_algorithm_checked(world: World) -> None:
    world.link_admin("mum")
    world.idp.sign_with_other_key = True  # signed by a key the keys document does not hold
    assert refused(world.sign_in(world.browser()))
    world.idp.sign_with_other_key = False
    world.idp.tamper = {"__alg": "none"}  # an unsigned token
    assert refused(world.sign_in(world.browser("198.51.100.21")))
    assert world.identities() == [(world.ids["mum"], ISSUER, world.idp.sub)]


def test_oidc_state_is_bound_to_the_browser_and_single_use(world: World) -> None:
    world.link_admin("mum")
    victim, attacker = world.browser("198.51.100.30"), world.browser("198.51.100.31")
    url = world.begin(victim)
    back = world.idp.authorize(url)
    # the attacker finishes the victim's flow from another browser: no flow cookie, refused
    assert refused(world.callback(attacker, back))
    # and a flow of the attacker's own cookie does not open the victim's state either
    world.begin(attacker)
    assert refused(world.callback(attacker, back))
    assert world.signed_in_as(attacker) is None
    # a state nobody issued
    other = world.idp.authorize(world.begin(world.browser("198.51.100.32")))
    forged = other.replace("state=", "state=x")
    assert refused(world.callback(world.browser("198.51.100.33"), forged))
    # single use: the same callback twice signs in once
    fresh = world.browser("198.51.100.34")
    back = world.idp.authorize(world.begin(fresh))
    assert world.callback(fresh, back).status_code == 303
    again = world.browser("198.51.100.35")
    again.cookies.update(fresh.cookies)
    assert refused(world.callback(again, back))


def test_oidc_provider_error_signs_nobody_in(world: World) -> None:
    browser = world.browser()
    world.begin(browser)
    answer = browser.get(f"{CALLBACK}?error=access_denied&state=whatever", follow_redirects=False)
    assert refused(answer)


def test_oidc_login_is_throttled_like_a_password(world: World) -> None:
    browser = world.browser("198.51.100.40")
    for _ in range(10):
        browser.get(f"{CALLBACK}?code=c&state=s", follow_redirects=False)
    blocked = browser.get(f"{CALLBACK}?code=c&state=s", follow_redirects=False)
    assert blocked.status_code == 429 and int(blocked.headers["Retry-After"]) >= 1


def test_oidc_child_profile_never_linked(world: World) -> None:
    kid = world.ids["kid"]
    # by an admin, from a fresh session
    by_admin = world.link_admin("kid")
    assert by_admin.status_code == 403 and by_admin.json()["code"] == "oidc_child_profile"
    # by the child, from their own fresh session
    assert world.send(world.kid, "POST", LINK).status_code == 403
    # a row that got there another way (a restore, an edit) never signs the child in
    with closing(sqlite3.connect(world.env.settings.database_path)) as conn:
        conn.execute(
            "INSERT INTO oidc_identity (id, user_id, issuer, sub, linked_at) "
            "VALUES ('01HZZZZZZZZZZZZZZZZZZZZZZA', ?, ?, ?, '2026-11-01 10:00:00')",
            (kid, ISSUER, world.idp.sub),
        )
        conn.commit()
    browser = world.browser()
    assert refused(world.sign_in(browser))
    assert world.signed_in_as(browser) is None
    # an account turned into a child profile after it was linked is refused the same way
    world.link_admin("dad")
    world.admin.patch(
        f"/api/v1/profiles/{world.ids['dad']}",
        json={"role": "child", "max_age_rating": 10},
        headers={CSRF_HEADER: world.env.csrf(world.admin)},
    )
    assert refused(world.sign_in(world.browser("198.51.100.50"), sub=world.idp.sub))


# --- linking ------------------------------------------------------------------------------------


def test_oidc_link_from_the_own_fresh_session(world: World) -> None:
    started = world.send(world.mum, "POST", LINK)
    assert started.status_code == 200
    url = started.json()["authorization_url"]
    assert url.startswith(f"{ISSUER}/authorize?")
    answer = world.callback(world.mum, world.idp.authorize(url))
    assert answer.status_code == 303 and "oidc_error" not in answer.headers["location"]
    assert world.identities() == [(world.ids["mum"], ISSUER, world.idp.sub)]
    assert "oidc.linked" in world.actions()
    # linked: the same person now signs in from another browser
    other = world.browser("198.51.100.60")
    assert world.sign_in(other).status_code == 303 and world.signed_in_as(other) == "mum"


def test_oidc_link_requires_fresh_session(world: World) -> None:
    world.env.clock.advance(timedelta(seconds=world.env.settings.fresh_auth_s + 60))
    stale = world.send(world.mum, "POST", LINK)
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    stale_admin = world.link_admin("mum")
    assert stale_admin.status_code == 401 and stale_admin.json()["code"] == "reauth_required"
    assert world.identities() == []
    # the password again: fresh, and it starts
    assert (
        world.send(
            world.mum,
            "POST",
            "/api/v1/auth/reauth",
            {"method": "password", "password": "correct horse battery staple"},
        ).status_code
        == 200
    )
    assert world.send(world.mum, "POST", LINK).status_code == 200


def test_oidc_link_is_refused_to_a_key_and_to_another_member(world: World) -> None:
    key = world.env.app.state.auth_gate.auth.create_api_key(  # type: ignore[attr-defined]
        name="k", username="admin", read_only=False
    ).value
    anonymous = world.browser("198.51.100.70")
    by_key = anonymous.post(LINK, headers={"Authorization": f"Bearer {key}"})
    assert by_key.status_code in (401, 403)
    assert by_key.status_code != 200
    put_by_key = anonymous.put(
        f"/api/v1/profiles/{world.ids['mum']}/oidc",
        json={"sub": "x"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert put_by_key.status_code in (401, 403)
    # IDOR: a member names another account
    theft = world.send(world.dad, "PUT", f"/api/v1/profiles/{world.ids['mum']}/oidc", {"sub": "x"})
    assert theft.status_code == 403
    assert (
        world.send(
            world.dad, "PUT", f"/api/v1/profiles/{world.ids['dad']}/oidc", {"sub": "x"}
        ).status_code
        == 403
    )
    # an anonymous caller
    assert anonymous.post(LINK).status_code in (401, 403)
    assert world.identities() == []


def test_oidc_link_finishes_for_the_account_that_started_it(world: World) -> None:
    started = world.send(world.mum, "POST", LINK).json()["authorization_url"]
    back = world.idp.authorize(started)
    # another browser, even with a session of its own, cannot finish mum's link
    assert refused(world.callback(world.dad, back))
    assert world.identities() == []
    # a session closed between the start and the end ends the link with it
    started = world.send(world.mum, "POST", LINK).json()["authorization_url"]
    back = world.idp.authorize(started)
    assert world.send(world.mum, "POST", "/api/v1/auth/logout").status_code == 204
    # the flow cookie stays in the jar, the session is gone: refused, back to the settings screen
    ended = world.callback(world.mum, back)
    assert ended.status_code == 303 and ended.headers["location"].endswith("oidc=refused")
    assert world.identities() == []


def test_oidc_a_sub_belongs_to_one_account(world: World) -> None:
    assert world.link_admin("mum").status_code == 204
    again = world.link_admin("dad")  # the same person on another account
    assert again.status_code == 409
    assert world.identities() == [(world.ids["mum"], ISSUER, world.idp.sub)]
    # the account has one identity per provider: another sub is a change, asked as one
    other = world.link_admin("mum", sub="another-person")
    assert other.status_code == 409
    assert world.link_admin("mum").status_code == 204  # the same link again changes nothing
    assert len(world.identities()) == 1


# --- the Fetcher and the guard -------------------------------------------------------------------


def test_oidc_discovery_goes_through_fetcher_ssrf_guard(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    provider = FakeOidcProvider()
    provider.resolver.table.update(
        {"internal.example.test": ["10.0.0.9"], "meta.example.test": ["169.254.169.254"]}
    )
    reached: list[str] = []

    def spy(request: httpx2.Request) -> httpx2.Response:
        reached.append(request.url.host)
        return provider._handle(request)

    network = Network(resolver=provider.resolver, transport=httpx2.MockTransport(spy))
    with world_for(monkeypatch, settings, provider=provider, network=network) as world:
        world.link_admin("mum")
        # the keys document is on a private address that nobody declared: refused before a request
        provider.discovery_overrides = {"jwks_uri": "https://internal.example.test/jwks"}
        assert refused(world.sign_in(world.browser()))
        # the token endpoint on the cloud metadata service: refused as well
        provider.discovery_overrides = {"token_endpoint": "https://meta.example.test/token"}
        assert refused(world.sign_in(world.browser("198.51.100.80")))
        assert "internal.example.test" not in reached and "meta.example.test" not in reached
        # a literal address is the guard's too
        provider.discovery_overrides = {"jwks_uri": "https://169.254.169.254/latest/keys"}
        assert refused(world.sign_in(world.browser("198.51.100.81")))
        assert "169.254.169.254" not in reached
        # the DNS answer was the one the guard validated: the names went through the resolver
        assert provider.host in {host for host, _port in provider.resolver.calls}
        assert world.identities() == [(world.ids["mum"], ISSUER, provider.sub)]


def test_oidc_discovery_of_another_issuer_is_refused(world: World) -> None:
    # a discovery document that names another issuer (mix-up) is not the configured provider
    world.idp.discovery_overrides = {"issuer": "https://elsewhere.example.test"}
    answer = world.browser().get(LOGIN, follow_redirects=False)
    assert answer.status_code == 303 and answer.headers["location"] == "/login?oidc_error=1"
    assert FLOW_COOKIE not in "".join(answer.headers.get_list("set-cookie"))


def test_oidc_provider_unreachable_is_a_refusal_not_an_error(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    provider = FakeOidcProvider()
    down = Network(
        resolver=FakeResolver({provider.host: [PUBLIC_IP]}),
        transport=httpx2.MockTransport(lambda _r: httpx2.Response(503)),
    )
    with world_for(monkeypatch, settings, provider=provider, network=down) as world:
        answer = world.browser().get(LOGIN, follow_redirects=False)
        assert answer.status_code == 303 and answer.headers["location"] == "/login?oidc_error=1"
        assert FLOW_COOKIE not in "".join(answer.headers.get_list("set-cookie"))
        # the password login is untouched
        assert world.env.login(client=world.browser("198.51.100.90")).status_code == 200


# --- the login page, the settings and the routes that keep their own credentials ---------------


def test_oidc_button_only_when_configured(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, world: World, tmp_path: Path
) -> None:
    page = world.browser().get("/login")
    assert page.status_code == 200 and LOGIN in page.text
    assert 'name="password"' in page.text  # the password stays
    assert "default-src" in page.headers["content-security-policy"]
    assert "unsafe-inline" not in page.headers["content-security-policy"]
    # not configured: no button, no route
    other = {"data_dir": tmp_path / "plain-data", "store_dir": tmp_path / "plain-store"}
    plain_settings = settings.model_copy(update={"public_url": None, **other})
    with auth_env(monkeypatch, plain_settings) as plain:
        client = plain.other_client("198.51.100.91")
        assert LOGIN not in client.get("/login").text
        assert client.get(LOGIN, follow_redirects=False).status_code == 404
        assert client.get(CALLBACK, follow_redirects=False).status_code == 404


def test_oidc_settings_are_checked() -> None:
    def load(**values: str) -> Any:
        env = {f"SHELFHOLD_{k.upper()}": v for k, v in values.items()}
        return load_settings(env).values

    full = {
        "public_url": "https://shelf.example.test",
        "oidc_issuer": ISSUER,
        "oidc_client_id": CLIENT_ID,
        "oidc_client_secret": "s" * 24,
    }
    assert load(**full).oidc_issuer == ISSUER
    assert load().oidc_issuer is None
    for broken in (
        {k: v for k, v in full.items() if k != "oidc_client_id"},  # a half setting
        {k: v for k, v in full.items() if k != "public_url"},  # no address to come back to
        {**full, "oidc_issuer": "http://idp.example.test"},  # clear text
        {**full, "oidc_issuer": "https://idp.example.test/?x=1"},  # a query is not an issuer
        {**full, "oidc_issuer": "https://user:pw@idp.example.test"},  # credentials in a URL
    ):
        with pytest.raises(Exception):  # noqa: B017
            load(**broken)
    assert "s" * 24 not in repr(load(**full))  # a secret never in a repr


def test_forward_auth_stays_refused_on_opds_kosync_and_lite(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    update = {
        "auth_forward_enabled": True,
        "http_trusted_proxies": ("10.0.0.0/8",),
    }
    with world_for(monkeypatch, settings, **update) as world:
        for path in ("/opds", "/kosync/users/auth", "/lite", "/lite/"):
            client = world.browser("10.1.2.3")
            answer = client.get(path, headers={"Remote-User": "admin"})
            assert answer.status_code == 403, path
            assert answer.headers["content-type"].startswith("application/problem+json")


def test_oidc_a_redirect_from_the_provider_is_never_followed(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    provider = FakeOidcProvider()
    provider.resolver.table["internal.example.test"] = ["10.0.0.9"]
    reached: list[str] = []

    def bounce(request: httpx2.Request) -> httpx2.Response:
        reached.append(request.url.host)
        return httpx2.Response(302, headers={"location": "https://internal.example.test/steal"})

    network = Network(resolver=provider.resolver, transport=httpx2.MockTransport(bounce))
    with world_for(monkeypatch, settings, provider=provider, network=network) as world:
        answer = world.browser().get(LOGIN, follow_redirects=False)
        assert answer.status_code == 303 and answer.headers["location"].startswith("/login")
        assert "internal.example.test" not in reached  # the discovery is not chased elsewhere
