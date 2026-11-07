"""M1b L05: passkeys (WebAuthn), EF-USR-025, ADR-0050, through the real `create_app()`.

A software authenticator (`tests/passkey_env.py`) signs the same bytes a browser would send, so
the server verifies genuine WebAuthn messages: challenge, origin, RP ID, flags, signature and
counter are the library's and ours, never a mock. The clock is moved by hand.
"""

import re
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.adapters.passwords import Argon2Hasher
from shelfhold.infrastructure.http_headers import NOSNIFF
from shelfhold.infrastructure.local_socket import call
from tests.auth_env import (
    ADMIN,
    CSRF_COOKIE,
    CSRF_HEADER,
    PASSWORD,
    SESSION_COOKIE,
    AuthEnv,
    auth_env,
    db_rows,
)
from tests.passkey_env import SoftPasskey, b64u

PASSKEYS = "/api/v1/auth/passkeys"
LOGIN_OPTIONS = f"{PASSKEYS}/login/options"
LOGIN = f"{PASSKEYS}/login"
REAUTH_OPTIONS = f"{PASSKEYS}/reauth/options"
REAUTH = "/api/v1/auth/reauth"
PURGE = "/api/v1/units/u1/purge"
IP = "198.51.100.7"
SECOND_ULID = "01HZZZZZZZZZZZZZZZZZZZZZZM"
ADD_MEMBER = (
    "INSERT INTO user (id, username, role, can_rate, password_hash) "
    "VALUES (?, 'member1', 'member', 0, ?)"
)

pytestmark = pytest.mark.req("EF-USR-025")


class _Library:
    def purge(self, **_: object) -> dict[str, str]:
        return {"ok": "purged"}


class _Services:
    library = _Library()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(
        monkeypatch, settings, ops_services=_Services(), public_url="https://testserver"
    ) as env:
        yield env


def register(
    api: AuthEnv, passkey: SoftPasskey | None = None, name: str = "Laptop", **create: Any
) -> tuple[SoftPasskey, Any]:
    passkey = passkey or SoftPasskey()
    options = api.post(f"{PASSKEYS}/options", json={})
    assert options.status_code == 200, options.text
    body = options.json()
    credential = passkey.create(body["options"], **create)
    done = api.post(PASSKEYS, json={"flow": body["flow"], "name": name, "credential": credential})
    return passkey, done


def login_flow(client: TestClient) -> dict[str, Any]:
    options = client.post(LOGIN_OPTIONS, json={})
    assert options.status_code == 200, options.text
    return dict(options.json())


def passkey_login(
    client: TestClient,
    passkey: SoftPasskey,
    *,
    flow: dict[str, Any] | None = None,
    **assertion: Any,
) -> Any:
    flow = flow or login_flow(client)
    credential = passkey.get(flow["options"], **assertion)
    return client.post(LOGIN, json={"flow": flow["flow"], "credential": credential})


def anonymous(api: AuthEnv, ip: str = IP) -> TestClient:
    return api.other_client(ip)


def registered(api: AuthEnv, **kwargs: Any) -> SoftPasskey:
    api.login()
    passkey, done = register(api, **kwargs)
    assert done.status_code == 201, done.text
    return passkey


def sql(settings: Settings, statement: str, *params: object) -> None:
    connection = sqlite3.connect(settings.database_path)
    try:
        with connection:
            connection.execute(statement, params)
    finally:
        connection.close()


def stored(settings: Settings, column: str) -> Any:
    assert column in {"sign_count", "revoked_at", "public_key", "last_used_at"}
    [(value,)] = db_rows(settings, f"SELECT {column} FROM webauthn_credential")  # noqa: S608
    return value


def audit_actions(settings: Settings) -> list[str]:
    return [row[0] for row in db_rows(settings, "SELECT action FROM audit ORDER BY at, id")]


# --- registration ------------------------------------------------------------------------------


def test_passkey_registration_stores_credential(api: AuthEnv) -> None:
    api.login()
    passkey, done = register(api, name="  Téléphone  ")
    assert done.status_code == 201, done.text
    listing = api.client.get(PASSKEYS).json()
    assert listing["available"] is True
    assert [item["name"] for item in listing["items"]] == ["Téléphone"]
    assert listing["items"][0]["last_used_at"] is None
    [(credential_id, public_key, count, transports)] = db_rows(
        api.settings,
        "SELECT credential_id, public_key, sign_count, transports FROM webauthn_credential",
    )
    assert credential_id == passkey.credential_id
    assert public_key and count == 0
    assert "internal" in str(transports)
    assert "passkey.registered" in audit_actions(api.settings)
    # nothing that identifies the credential reaches the audit trail or the API answers
    audit = "\n".join(str(r) for r in db_rows(api.settings, "SELECT target, action FROM audit"))
    assert b64u(passkey.credential_id) not in audit
    assert b64u(passkey.credential_id) not in str(listing)


def test_registration_demands_resident_key_and_user_verification(api: AuthEnv) -> None:
    api.login()
    options = api.post(f"{PASSKEYS}/options", json={}).json()["options"]
    selection = options["authenticatorSelection"]
    assert selection["residentKey"] == "required"
    assert selection["userVerification"] == "required"
    assert options["attestation"] == "none"
    assert options["rp"]["id"] == "testserver"
    login = login_flow(anonymous(api))["options"]
    assert login["userVerification"] == "required"
    assert not login.get("allowCredentials")  # discoverable: no identifier typed


def test_registration_refuses_an_authenticator_without_user_verification(api: AuthEnv) -> None:
    api.login()
    _, done = register(api, SoftPasskey(uv=False))
    assert done.status_code == 422
    assert db_rows(api.settings, "SELECT count(*) FROM webauthn_credential") == [(0,)]


def test_registration_needs_a_session_not_a_key_and_a_csrf_token(api: AuthEnv) -> None:
    assert api.client.post(f"{PASSKEYS}/options", json={}).status_code == 401
    api.login()
    no_csrf = api.client.post(f"{PASSKEYS}/options", json={}, headers={CSRF_HEADER: "nope"})
    assert no_csrf.status_code == 403


def test_the_same_authenticator_cannot_be_registered_twice(api: AuthEnv) -> None:
    passkey = registered(api)
    _, again = register(api, passkey, name="Again")
    assert again.status_code == 422
    assert db_rows(api.settings, "SELECT count(*) FROM webauthn_credential") == [(1,)]
    options = api.post(f"{PASSKEYS}/options", json={}).json()["options"]
    assert [c["id"] for c in options["excludeCredentials"]] == [b64u(passkey.credential_id)]


@pytest.mark.parametrize("name", ["", "   ", "x" * 65])
def test_registration_name_is_bounded(api: AuthEnv, name: str) -> None:
    api.login()
    _, done = register(api, name=name)
    assert done.status_code == 422


def test_passkeys_per_account_are_capped(api: AuthEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("shelfhold.identity.application.passkeys.MAX_PASSKEYS", 2)
    api.login()
    assert register(api, name="a")[1].status_code == 201
    assert register(api, name="b")[1].status_code == 201
    assert api.post(f"{PASSKEYS}/options", json={}).status_code == 422


# --- challenge ---------------------------------------------------------------------------------


def test_challenge_is_random_per_flow_and_long_enough(api: AuthEnv) -> None:
    client = anonymous(api)
    challenges = {login_flow(client)["options"]["challenge"] for _ in range(5)}
    assert len(challenges) == 5
    assert all(len(c) >= 43 for c in challenges)  # 32 bytes in base64url


def test_a_replayed_assertion_fails(api: AuthEnv) -> None:
    passkey = registered(api)
    client = anonymous(api)
    flow = login_flow(client)
    credential = passkey.get(flow["options"])
    body = {"flow": flow["flow"], "credential": credential}
    assert client.post(LOGIN, json=body).status_code == 200
    replay = anonymous(api, "198.51.100.8")
    assert replay.post(LOGIN, json=body).status_code == 401
    # the same challenge, a fresh signature: the flow is consumed too
    assert (
        replay.post(LOGIN, json={**body, "credential": passkey.get(flow["options"])}).status_code
        == 401
    )


def test_a_replayed_registration_fails(api: AuthEnv) -> None:
    api.login()
    body = api.post(f"{PASSKEYS}/options", json={}).json()
    credential = SoftPasskey().create(body["options"])
    payload = {"flow": body["flow"], "name": "x", "credential": credential}
    assert api.post(PASSKEYS, json=payload).status_code == 201
    assert api.post(PASSKEYS, json=payload).status_code == 422


def test_a_challenge_expires(api: AuthEnv) -> None:
    passkey = registered(api)
    client = anonymous(api)
    flow = login_flow(client)
    api.clock.advance(timedelta(minutes=3))
    assert passkey_login(client, passkey, flow=flow).status_code == 401
    api.login()
    options = api.post(f"{PASSKEYS}/options", json={}).json()
    api.clock.advance(timedelta(minutes=3))
    stale = api.post(
        PASSKEYS,
        json={
            "flow": options["flow"],
            "name": "x",
            "credential": SoftPasskey().create(options["options"]),
        },
    )
    assert stale.status_code == 422


def test_a_challenge_is_bound_to_its_purpose_and_its_session(api: AuthEnv) -> None:
    passkey = registered(api)
    client = anonymous(api)
    # a registration challenge cannot open a session
    reg = api.post(f"{PASSKEYS}/options", json={}).json()
    credential = passkey.get(reg["options"])
    assert (
        client.post(LOGIN, json={"flow": reg["flow"], "credential": credential}).status_code == 401
    )
    # a login challenge cannot register
    flow = login_flow(client)
    registration = api.post(f"{PASSKEYS}/options", json={}).json()["options"]
    refused = api.post(
        PASSKEYS,
        json={"flow": flow["flow"], "name": "x", "credential": SoftPasskey().create(registration)},
    )
    assert refused.status_code == 422
    # a registration challenge of one session is useless to another
    mine = api.post(f"{PASSKEYS}/options", json={}).json()
    second = api.other_client("198.51.100.20")
    assert api.login(client=second).status_code == 200
    stolen = api.post(
        PASSKEYS,
        client=second,
        json={
            "flow": mine["flow"],
            "name": "x",
            "credential": SoftPasskey().create(mine["options"]),
        },
    )
    assert stolen.status_code == 422
    # a reauthentication challenge cannot be used to log in either
    fresh = api.post(REAUTH_OPTIONS, json={}).json()
    assert passkey_login(client, passkey, flow=fresh).status_code == 401


def test_unknown_or_malformed_flows_are_refused(api: AuthEnv) -> None:
    passkey = registered(api)
    client = anonymous(api)
    flow = login_flow(client)
    credential = passkey.get(flow["options"])
    for bad in ("", "x" * 5000, "not-a-flow", None):
        assert client.post(LOGIN, json={"flow": bad, "credential": credential}).status_code in (
            401,
            422,
        )


# --- origin and RP ID --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("origin", "rp_id"),
    [
        ("https://evil.example", "testserver"),
        ("https://sub.testserver", "testserver"),
        ("https://testserver.evil.example", "testserver"),
        ("http://testserver", "testserver"),
        ("https://testserver:8443", "testserver"),
        ("https://testserver", "sub.testserver"),
        ("https://sub.testserver", "sub.testserver"),
        ("https://testserver", "evil.example"),
    ],
)
def test_origin_and_rp_id_are_checked_strictly(api: AuthEnv, origin: str, rp_id: str) -> None:
    api.login()
    _, done = register(api, SoftPasskey(origin=origin, rp_id=rp_id))
    assert done.status_code == 422  # at registration
    good = registered(api)  # a genuine credential
    forged = SoftPasskey(
        origin=origin, rp_id=rp_id, credential_id=good.credential_id, key=good.key,
        user_handle=good.user_handle,
    )  # fmt: skip
    assert passkey_login(anonymous(api), forged).status_code == 401  # at login, with its key


def test_a_cross_origin_assertion_is_refused(api: AuthEnv) -> None:
    api.login()
    _, done = register(api, SoftPasskey(), cross_origin=True)
    assert done.status_code == 422
    passkey = registered(api)
    assert passkey_login(anonymous(api), passkey, cross_origin=True).status_code == 401


def test_rp_id_and_origin_come_from_the_public_url(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    url = "https://shelf.example.org:8443/ignored/path"
    with auth_env(monkeypatch, settings, public_url=url) as api:
        api.login()
        options = api.post(f"{PASSKEYS}/options", json={}).json()["options"]
        assert options["rp"]["id"] == "shelf.example.org"
        right = SoftPasskey(origin="https://shelf.example.org:8443", rp_id="shelf.example.org")
        assert register(api, right)[1].status_code == 201
        assert register(api, SoftPasskey())[1].status_code == 422  # the Host of the request: no
        client = anonymous(api)
        assert passkey_login(client, right).status_code == 200
        # a request Host header says nothing about the RP ID
        flow = client.post(LOGIN_OPTIONS, json={}, headers={"Host": "evil.example"}).json()
        assert flow["options"]["rpId"] == "shelf.example.org"


# --- authenticator requirements ----------------------------------------------------------------


def test_login_refuses_an_assertion_without_user_verification(api: AuthEnv) -> None:
    passkey = registered(api)
    passkey.uv = False
    assert passkey_login(anonymous(api), passkey).status_code == 401


# --- login and session -------------------------------------------------------------------------


def test_passkey_discoverable_login_opens_session(api: AuthEnv) -> None:
    passkey = registered(api)
    api.clock.advance(timedelta(hours=2))
    client = anonymous(api)
    response = passkey_login(client, passkey)  # no username anywhere in the exchange
    assert response.status_code == 200, response.text
    assert response.json()["csrf_token"] == client.cookies.get(CSRF_COOKIE)
    session = client.get("/api/v1/auth/session").json()
    assert session["username"] == ADMIN
    assert session["credential"] == "session"
    assert session["auth_time"].startswith(api.clock.now().strftime("%Y-%m-%dT%H:%M"))
    [(last_used,)] = db_rows(api.settings, "SELECT last_used_at FROM webauthn_credential")
    assert last_used is not None
    assert "auth.passkey_login" in audit_actions(api.settings)
    cookie = next(c for c in response.headers.get_list("set-cookie") if c.startswith("__Host-sid="))
    assert "HttpOnly" in cookie and "Secure" in cookie and "SameSite=Strict" in cookie


def test_passkey_login_renews_the_session_identifier(api: AuthEnv) -> None:
    passkey = registered(api)
    client = anonymous(api)
    assert api.login(client=client).status_code == 200  # a session already held by the browser
    before = client.cookies[SESSION_COOKIE]
    held = {SESSION_COOKIE: before}
    assert passkey_login(client, passkey).status_code == 200
    after = client.cookies[SESSION_COOKIE]
    assert after != before  # never adopted (fixation)
    old = anonymous(api, "198.51.100.30")
    old.cookies.set(SESSION_COOKIE, held[SESSION_COOKIE])
    assert old.get("/api/v1/auth/session").status_code == 401  # and the old one is closed
    # a session id planted by an attacker before the login is not adopted either
    planted = anonymous(api, "198.51.100.31")
    flow = login_flow(planted)
    forced = planted.post(
        LOGIN,
        json={"flow": flow["flow"], "credential": passkey.get(flow["options"])},
        headers={"Cookie": f"{SESSION_COOKIE}=attacker-chosen-value"},
    )
    assert forced.status_code == 200
    issued = [c for c in forced.headers.get_list("set-cookie") if c.startswith(SESSION_COOKIE)]
    assert issued and "attacker-chosen-value" not in issued[0]


def test_passkey_login_respects_the_login_throttle(api: AuthEnv) -> None:
    passkey = registered(api)
    client = anonymous(api, "198.51.100.40")
    for _ in range(10):
        bad = client.post(LOGIN, json={"flow": "f" * 43, "credential": {}})
        assert bad.status_code in (401, 422)
    blocked = passkey_login(client, passkey)
    assert blocked.status_code == 429
    assert int(blocked.headers["Retry-After"]) >= 1
    api.clock.advance(timedelta(minutes=2))
    assert passkey_login(client, passkey).status_code == 200


def test_unknown_credential_and_revoked_credential_answer_alike(api: AuthEnv) -> None:
    passkey = registered(api)
    stranger = SoftPasskey()
    stranger.user_handle = passkey.user_handle
    unknown = passkey_login(anonymous(api), stranger)
    [(item_id,)] = db_rows(api.settings, "SELECT id FROM webauthn_credential")
    assert (
        api.client.delete(f"{PASSKEYS}/{item_id}", headers={CSRF_HEADER: api.csrf()}).status_code
        == 204
    )
    revoked = passkey_login(anonymous(api, "198.51.100.50"), passkey)
    assert unknown.status_code == revoked.status_code == 401
    assert unknown.json()["code"] == revoked.json()["code"] == "invalid_credentials"


def test_a_mismatched_user_handle_is_refused(api: AuthEnv) -> None:
    passkey = registered(api)
    client = anonymous(api)
    flow = login_flow(client)
    credential = passkey.get(flow["options"])
    credential["response"]["userHandle"] = b64u(b"someone-else")
    assert (
        client.post(LOGIN, json={"flow": flow["flow"], "credential": credential}).status_code == 401
    )


# --- counter -----------------------------------------------------------------------------------


def test_passkey_sign_count_regression_rejected(api: AuthEnv) -> None:
    passkey = registered(api)
    assert passkey_login(anonymous(api), passkey, counter=5).status_code == 200
    assert stored(api.settings, "sign_count") == 5
    for counter in (3, 5, 0):  # backwards, repeated, and a counter that fell to zero
        refused = passkey_login(anonymous(api, "198.51.100.60"), passkey, counter=counter)
        assert refused.status_code == 401
    assert stored(api.settings, "sign_count") == 5  # the stored counter never moves back
    assert "auth.passkey_counter_regression" in audit_actions(api.settings)
    assert passkey_login(anonymous(api, "198.51.100.61"), passkey, counter=6).status_code == 200


def test_a_counter_that_stays_at_zero_is_tolerated(api: AuthEnv) -> None:
    passkey = registered(api, passkey=SoftPasskey(counter_step=0))  # synced passkeys never count
    for n in range(3):
        assert passkey_login(anonymous(api, f"198.51.100.{70 + n}"), passkey).status_code == 200
    assert stored(api.settings, "sign_count") == 0


# --- revocation and last factor ----------------------------------------------------------------


def test_revoked_passkey_rejected(api: AuthEnv) -> None:
    passkey = registered(api)
    [(item_id,)] = db_rows(api.settings, "SELECT id FROM webauthn_credential")
    gone = api.client.delete(f"{PASSKEYS}/{item_id}", headers={CSRF_HEADER: api.csrf()})
    assert gone.status_code == 204
    assert api.client.get(PASSKEYS).json()["items"] == []
    assert passkey_login(anonymous(api), passkey).status_code == 401
    flow = api.post(REAUTH_OPTIONS, json={}).json()
    reauth = api.post(
        REAUTH,
        json={
            "method": "passkey",
            "flow": flow["flow"],
            "credential": passkey.get(flow["options"]),
        },
    )
    assert reauth.status_code == 401
    assert stored(api.settings, "revoked_at") is not None  # the history stays
    assert "passkey.revoked" in audit_actions(api.settings)
    again = api.client.delete(f"{PASSKEYS}/{item_id}", headers={CSRF_HEADER: api.csrf()})
    assert again.status_code == 404


def test_one_cannot_revoke_the_passkey_of_someone_else(api: AuthEnv) -> None:
    registered(api)
    [(item_id,)] = db_rows(api.settings, "SELECT id FROM webauthn_credential")
    digest = Argon2Hasher().hash(PASSWORD)
    sql(
        api.settings,
        ADD_MEMBER,
        SECOND_ULID,
        digest,
    )
    other = api.other_client("198.51.100.80")
    assert api.login("member1", client=other).status_code == 200
    gone = other.delete(f"{PASSKEYS}/{item_id}", headers={CSRF_HEADER: api.csrf(other)})
    assert gone.status_code == 404
    assert other.get(PASSKEYS).json()["items"] == []  # nor does the list show it
    assert stored(api.settings, "revoked_at") is None


def test_cannot_remove_last_login_factor(api: AuthEnv) -> None:
    registered(api)
    _, second = register(api, name="Second")
    assert second.status_code == 201
    sql(api.settings, "UPDATE user SET password_hash = NULL")  # a passkey-only account
    ids = [r[0] for r in db_rows(api.settings, "SELECT id FROM webauthn_credential ORDER BY id")]
    headers = {CSRF_HEADER: api.csrf()}
    assert api.client.delete(f"{PASSKEYS}/{ids[0]}", headers=headers).status_code == 204
    last = api.client.delete(f"{PASSKEYS}/{ids[1]}", headers=headers)
    assert last.status_code == 409
    assert last.json()["code"] == "last_login_factor"
    assert db_rows(
        api.settings, "SELECT count(*) FROM webauthn_credential WHERE revoked_at IS NULL"
    ) == [(1,)]


def test_with_a_password_the_last_passkey_can_go(api: AuthEnv) -> None:
    registered(api)
    [(item_id,)] = db_rows(api.settings, "SELECT id FROM webauthn_credential")
    gone = api.client.delete(f"{PASSKEYS}/{item_id}", headers={CSRF_HEADER: api.csrf()})
    assert gone.status_code == 204


def test_admin_reset_password_also_revokes_the_passkeys(api: AuthEnv) -> None:
    passkey = registered(api)
    closed = call(
        api.settings.data_dir / "shelfhold.sock",
        "identity.reset_password",
        {"username": ADMIN, "password": "an entirely new passphrase"},
    )
    assert closed == 1
    assert stored(api.settings, "revoked_at") is not None
    assert passkey_login(anonymous(api), passkey).status_code == 401
    assert "user.passkeys_revoked" in audit_actions(api.settings)
    assert api.login(password="an entirely new passphrase").status_code == 200  # noqa: S106


# --- re-authentication -------------------------------------------------------------------------


def test_reauth_with_passkey_unblocks_reserved_action(api: AuthEnv) -> None:
    passkey = registered(api)
    api.clock.advance(timedelta(minutes=20))
    stale = api.post(PURGE, json={"typed": "x"})
    assert stale.status_code == 401
    assert "passkey" in stale.json()["reauth_methods"]
    old_token = api.client.cookies[SESSION_COOKIE]
    flow = api.post(REAUTH_OPTIONS, json={}).json()
    assert flow["options"]["userVerification"] == "required"
    assert [c["id"] for c in flow["options"]["allowCredentials"]] == [b64u(passkey.credential_id)]
    done = api.post(
        REAUTH,
        json={
            "method": "passkey",
            "flow": flow["flow"],
            "credential": passkey.get(flow["options"]),
        },
    )
    assert done.status_code == 200, done.text
    assert api.client.cookies[SESSION_COOKIE] != old_token  # renewed, as for a password
    assert api.post(PURGE, json={"typed": "x"}).status_code == 200
    [(session_id, auth_time)] = db_rows(
        api.settings, "SELECT session_id, auth_time FROM audit WHERE action = 'purge.now'"
    )
    assert session_id and auth_time
    assert "auth.reauth" in audit_actions(api.settings)


def test_reauth_with_the_passkey_of_another_account_is_refused(api: AuthEnv) -> None:
    api.login()
    digest = Argon2Hasher().hash(PASSWORD)
    sql(
        api.settings,
        ADD_MEMBER,
        SECOND_ULID,
        digest,
    )
    other = api.other_client("198.51.100.90")
    assert api.login("member1", client=other).status_code == 200
    theirs = SoftPasskey()
    _, done = register_with(api, other, theirs)
    assert done.status_code == 201
    flow = api.post(REAUTH_OPTIONS, json={}).json()
    credential = theirs.get(flow["options"])
    refused = api.post(
        REAUTH, json={"method": "passkey", "flow": flow["flow"], "credential": credential}
    )
    assert refused.status_code == 401


def register_with(
    api: AuthEnv, client: TestClient, passkey: SoftPasskey
) -> tuple[SoftPasskey, Any]:
    body = api.post(f"{PASSKEYS}/options", client=client, json={}).json()
    credential = passkey.create(body["options"])
    done = api.post(
        PASSKEYS,
        client=client,
        json={"flow": body["flow"], "name": "Theirs", "credential": credential},
    )
    return passkey, done


def test_reauth_with_passkey_respects_the_attempt_throttle(api: AuthEnv) -> None:
    passkey = registered(api)
    for _ in range(10):
        bad = api.post(REAUTH, json={"method": "passkey", "flow": "f" * 43, "credential": {}})
        assert bad.status_code in (401, 422)
    flow = api.post(REAUTH_OPTIONS, json={}).json()
    blocked = api.post(
        REAUTH,
        json={
            "method": "passkey",
            "flow": flow["flow"],
            "credential": passkey.get(flow["options"]),
        },
    )
    assert blocked.status_code in (401, 429)


# --- secure context ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "public_url",
    [
        None,
        "http://192.168.1.10:8080",
        "http://shelf.lan",
        "https://192.168.1.10",
        "ftp://shelf.example",
    ],
)
def test_passkey_requires_secure_context(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, public_url: str | None
) -> None:
    with auth_env(monkeypatch, settings, public_url=public_url) as api:
        api.login()
        listing = api.client.get(PASSKEYS).json()
        assert listing["available"] is False
        assert listing["reason"]
        refused = api.post(f"{PASSKEYS}/options", json={})
        assert refused.status_code == 409
        assert refused.json()["code"] == "passkey_unavailable"
        anon = anonymous(api)
        assert anon.post(LOGIN_OPTIONS, json={}).status_code == 409
        assert anon.post(LOGIN, json={"flow": "x", "credential": {}}).status_code in (401, 409)
        page = anon.get("/login")
        assert "passkey" not in page.text.lower()  # the function is absent from the page
        assert (
            "<button" in page.text and page.text.count("<button") == 1
        )  # the password button only
        assert anon.get("/login/passkey.js").status_code == 404
        assert "script-src 'self'" not in page.headers["content-security-policy"]
        assert api.post(
            REAUTH, json={"method": "passkey", "flow": "x", "credential": {}}
        ).status_code in (
            401,
            409,
            422,
        )


def test_localhost_over_http_is_a_secure_context(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    base = "http://localhost:8080"
    with auth_env(monkeypatch, settings, public_url=base, base_url=base) as api:
        assert api.login().status_code == 200  # plain cookies over http: `csrf`, not `__Host-`
        headers = {CSRF_HEADER: api.client.cookies["csrf"]}
        assert api.client.get(PASSKEYS).json()["available"] is True
        local = SoftPasskey(origin=base, rp_id="localhost")
        body = api.client.post(f"{PASSKEYS}/options", json={}, headers=headers).json()
        assert body["options"]["rp"]["id"] == "localhost"
        done = api.client.post(
            PASSKEYS,
            json={
                "flow": body["flow"],
                "name": "Local",
                "credential": local.create(body["options"]),
            },
            headers=headers,
        )
        assert done.status_code == 201
        plain = api.other_client(IP, base)
        assert passkey_login(plain, local).status_code == 200


# --- login page --------------------------------------------------------------------------------


def test_login_page_csp_still_has_no_unsafe_inline(api: AuthEnv) -> None:
    page = api.other_client(IP).get("/login")
    csp = page.headers["content-security-policy"]
    assert "unsafe-inline" not in csp and "unsafe-eval" not in csp
    directives = dict(d.strip().split(" ", 1) for d in csp.split(";") if " " in d.strip())
    scripts = directives["script-src"].split()
    assert "'self'" in scripts
    assert all(s == "'self'" or s.startswith("'sha256-") for s in scripts)
    assert directives["default-src"] == "'none'"
    assert directives["connect-src"] == "'self'"
    assert directives["frame-ancestors"] == "'none'"
    # the only inline script is the unchanged one; the passkey code is a same-origin file
    inline = re.findall(r"<script>(.*?)</script>", page.text, flags=re.DOTALL)
    assert len(inline) == 1 and "navigator.credentials" not in inline[0]
    sources = re.findall(r'<script[^>]*\ssrc="([^"]*)"', page.text)
    assert sources == ["/login/passkey.js"]
    assert not re.search(
        r"\son\w+=", re.sub(r"<script.*?</script>", "", page.text, flags=re.DOTALL)
    )
    assert "javascript:" not in page.text
    for needle in ("og:", "twitter:", "<img", "shelfhold", "kavita"):
        assert needle not in re.sub(r'href="[^"]*"', "", page.text.lower())  # still neutral


def test_passkey_script_is_served_from_the_same_origin(api: AuthEnv) -> None:
    script = api.other_client(IP).get("/login/passkey.js")
    assert script.status_code == 200
    assert script.headers["content-type"].startswith("text/javascript")
    assert script.headers["x-content-type-options"] == NOSNIFF
    assert (
        "no-cache" in script.headers["cache-control"]
        or "no-store" in script.headers["cache-control"]
    )
    text = script.text
    assert "navigator.credentials" in text
    assert "eval(" not in text and "innerHTML" not in text
    assert "http://" not in text and "https://" not in text  # no other origin, ever


def test_the_passkey_secrets_are_never_stored_in_clear_in_responses(api: AuthEnv) -> None:
    passkey = registered(api)
    everything = api.client.get(PASSKEYS).text + str(api.client.get("/api/v1/auth/session").json())
    public_key = stored(api.settings, "public_key")
    assert b64u(public_key) not in everything and b64u(passkey.credential_id) not in everything


# --- review PR #95: freshness of the registration and the revocation ---------------------------


def test_passkey_register_and_revoke_need_fresh_auth_in_the_hardened_profile(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings, public_url="https://testserver", hardened=True) as api:
        assert api.login(remember=False).status_code == 200
        passkey, done = register(api)
        assert done.status_code == 201  # fresh: the session was just opened
        [(item_id,)] = db_rows(api.settings, "SELECT id FROM webauthn_credential")
        api.clock.advance(timedelta(minutes=20))
        stale = api.post(f"{PASSKEYS}/options", json={})
        assert stale.status_code == 401
        assert stale.json()["type"].endswith("/reauth-required")
        gone = api.client.delete(f"{PASSKEYS}/{item_id}", headers={CSRF_HEADER: api.csrf()})
        assert gone.status_code == 401
        assert stored(api.settings, "revoked_at") is None  # nothing happened
        flow = api.post(REAUTH_OPTIONS, json={}).json()
        credential = passkey.get(flow["options"])
        reauth = api.post(
            REAUTH, json={"method": "passkey", "flow": flow["flow"], "credential": credential}
        )
        assert reauth.status_code == 200
        gone = api.client.delete(f"{PASSKEYS}/{item_id}", headers={CSRF_HEADER: api.csrf()})
        assert gone.status_code in (204, 409)
        rows = db_rows(
            api.settings,
            "SELECT action, session_id, auth_time FROM audit "
            "WHERE action IN ('passkey.register', 'passkey.revoke')",
        )
        assert {r[0] for r in rows} == {"passkey.register", "passkey.revoke"}
        assert all(r[1] and r[2] for r in rows)  # session_id and auth_time on each


def test_passkey_register_and_revoke_have_no_freshness_by_default(api: AuthEnv) -> None:
    registered(api)
    api.clock.advance(timedelta(hours=3))  # the default session lasts 30 days
    assert register(api, name="Later")[1].status_code == 201
    rows = db_rows(
        api.settings, "SELECT session_id, auth_time FROM audit WHERE action = 'passkey.register'"
    )
    assert len(rows) >= 2 and all(r[0] and r[1] for r in rows)  # audited either way


def test_a_key_can_neither_register_nor_revoke_a_passkey(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    from tests.auth_env import run_cli

    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "create") == 0
    key = re.search(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", capsys.readouterr().out)
    assert key
    headers = {"Authorization": f"Bearer {key.group(0)}"}
    options = api.client.post(f"{PASSKEYS}/options", json={}, headers=headers)
    assert options.status_code == 403 and options.json()["code"] == "credential_not_allowed"
    assert api.client.delete(f"{PASSKEYS}/x", headers=headers).status_code == 403


# --- review PR #95: sessions of a revoked passkey, atomic reset --------------------------------


def test_revoking_a_passkey_closes_the_sessions_it_opened(api: AuthEnv) -> None:
    passkey = registered(api)
    other = anonymous(api, "198.51.100.110")
    assert passkey_login(other, passkey).status_code == 200
    assert other.get("/api/v1/auth/session").status_code == 200
    [(item_id,)] = db_rows(api.settings, "SELECT id FROM webauthn_credential")
    assert db_rows(api.settings, "SELECT count(*) FROM session WHERE passkey_id = ?", item_id) == [
        (1,)
    ]
    gone = api.client.delete(f"{PASSKEYS}/{item_id}", headers={CSRF_HEADER: api.csrf()})
    assert gone.status_code == 204
    assert other.get("/api/v1/auth/session").status_code == 401  # the stolen device is out
    assert api.client.get("/api/v1/auth/session").status_code == 200  # the password session stays


def test_reset_password_is_one_transaction_a_passkey_login_cannot_slip_through(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.identity.adapters.store import SqlIdentityStore

    passkey = registered(api)
    real = SqlIdentityStore.insert_session
    sock = api.settings.data_dir / "shelfhold.sock"

    def reset_first(self: SqlIdentityStore, session: Any) -> None:
        # the assertion was verified; the host resets the account before the session is written
        call(sock, "identity.reset_password", {"username": ADMIN, "password": "a new passphrase!"})
        real(self, session)

    monkeypatch.setattr(SqlIdentityStore, "insert_session", reset_first)
    client = anonymous(api, "198.51.100.111")
    refused = passkey_login(client, passkey)
    assert refused.status_code == 401
    assert db_rows(api.settings, "SELECT count(*) FROM session WHERE revoked_at IS NULL") == [(0,)]
    monkeypatch.undo()


# --- review PR #95: `status` and the RP ID of the passkeys -------------------------------------


def test_status_warns_when_the_rp_id_is_no_longer_the_one_of_the_passkeys(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from tests.auth_env import run_cli

    registered(api)
    assert stored_rp_id(api.settings) == "testserver"  # recorded at registration
    monkeypatch.setenv("SHELFHOLD_PUBLIC_URL", "https://testserver:8443")  # same domain
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "status", "--json") == 0
    assert "passkey_rp_changed" not in capsys.readouterr().out
    for url in ("https://other.example.org", "http://192.168.1.4:8080"):  # new domain, no domain
        monkeypatch.setenv("SHELFHOLD_PUBLIC_URL", url)
        assert run_cli(monkeypatch, api.settings, "status", "--json") == 0
        out = capsys.readouterr().out
        assert "passkey_rp_changed" in out and "testserver" in out


def stored_rp_id(settings: Settings) -> Any:
    [(value,)] = db_rows(settings, "SELECT rp_id FROM webauthn_credential")
    return value
