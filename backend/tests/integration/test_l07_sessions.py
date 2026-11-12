"""M3 L07 (EF-USR-011, EF-USR-023): the list of sessions and their revocation, the way back in by
the host CLI only, and the printable recovery kit that holds no secret.

Attacks: a child, a member, an API key and a device token against every route; one profile
revoking the session of another (the answer is the one of an id that does not exist); an admin
route without its guards; a recovery code asked from the web; the bytes of the keyring and the
passwords searched in everything the kit and the lists say.
"""

from __future__ import annotations

import base64
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.infrastructure.secrets import diagnostics
from tests.auth_env import (
    ADMIN,
    CSRF_COOKIE,
    CSRF_HEADER,
    PASSWORD,
    SESSION_COOKIE,
    AuthEnv,
    auth_env,
    db_rows,
    run_cli,
)

SESSIONS = "/api/v1/auth/sessions"
OTHERS = f"{SESSIONS}/revoke-others"
ADMIN_SESSIONS = "/api/v1/admin/sessions"
KIT = "/api/v1/admin/recovery-kit"
BROWSER = "Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0"
PHONE = "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile Safari"
NEW_PASSWORD = "an entirely new passphrase"  # noqa: S105


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _user(
    env: AuthEnv, name: str, role: str, ip: str, agent: str = BROWSER, **kw: Any
) -> tuple[TestClient, str]:
    profile_id = env.app.state.auth_gate.auth.profile_add(name, PASSWORD, role, **kw)  # type: ignore[attr-defined]
    client = env.other_client(ip)
    done = client.post(
        "/api/v1/auth/login",
        json={"username": name, "password": PASSWORD},
        headers={"User-Agent": agent},
    )
    assert done.status_code == 200
    return client, profile_id


def _admin(env: AuthEnv, agent: str = BROWSER) -> TestClient:
    client = env.other_client("10.0.0.1")
    done = client.post(
        "/api/v1/auth/login",
        json={"username": ADMIN, "password": PASSWORD},
        headers={"User-Agent": agent},
    )
    assert done.status_code == 200
    return client


def _send(env: AuthEnv, client: TestClient, method: str, path: str, **kw: Any) -> Any:
    return client.request(method, path, headers={CSRF_HEADER: env.csrf(client)}, **kw)


def _key(env: AuthEnv, username: str, **kw: Any) -> str:
    return env.app.state.auth_gate.auth.create_api_key(name="k", username=username, **kw).value  # type: ignore[attr-defined,no-any-return]


@pytest.mark.req("EF-USR-011")
def test_sessions_list_and_revoke(api: AuthEnv) -> None:
    """The list shows the device, `auth_time` and the last activity, and never a token."""
    laptop = _admin(api)
    phone = api.other_client("10.0.0.9")
    assert (
        phone.post(
            "/api/v1/auth/login",
            json={"username": ADMIN, "password": PASSWORD},
            headers={"User-Agent": PHONE},
        ).status_code
        == 200
    )
    api.clock.advance(timedelta(minutes=5))
    listed = laptop.get(SESSIONS)
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 2
    assert sum(1 for i in items if i["current"]) == 1
    labels = {i["device"] for i in items}
    assert any("Firefox" in label for label in labels)
    assert any("iPhone" in label for label in labels)
    for item in items:
        assert {"id", "device", "auth_time", "last_seen_at", "created_at", "current"} <= set(item)
    assert "Mozilla/5.0" not in listed.text  # a label, not the raw header
    assert "token" not in listed.text and "csrf" not in listed.text
    other = next(i for i in items if not i["current"])
    gone = _send(api, laptop, "DELETE", f"{SESSIONS}/{other['id']}")
    assert gone.status_code == 204
    assert phone.get("/api/v1/me").status_code == 401  # the revoked one is out at its next call
    assert len(laptop.get(SESSIONS).json()["items"]) == 1
    again = _send(api, laptop, "DELETE", f"{SESSIONS}/{other['id']}")
    assert again.status_code == 404  # revoking twice is the answer of an unknown id


@pytest.mark.req("EF-USR-011")
def test_revoke_all_other_sessions_keeps_the_current_one(api: AuthEnv) -> None:
    laptop = _admin(api)
    for ip in ("10.0.0.7", "10.0.0.8"):
        assert api.login(client=api.other_client(ip)).status_code == 200
    assert len(laptop.get(SESSIONS).json()["items"]) == 3
    done = _send(api, laptop, "POST", OTHERS)
    assert done.status_code == 200 and done.json() == {"revoked": 2}
    assert len(laptop.get(SESSIONS).json()["items"]) == 1
    assert laptop.get("/api/v1/me").status_code == 200
    audit = db_rows(
        api.settings, "SELECT actor_id, target FROM audit WHERE action = ?", "auth.sessions_revoked"
    )
    assert audit and all("*" not in str(row[1]) for row in audit[-1:])


@pytest.mark.req("EF-USR-011", "ENF-SEC-010")
def test_session_revoke_idor_is_the_answer_of_an_unknown_id(api: AuthEnv) -> None:
    """A member cannot see, list or close the session of another profile, the admin's included;
    the answer is that of an id that does not exist, so ids cannot be probed."""
    mum, _ = _user(api, "mum", "member", "10.0.0.3")
    kid, _ = _user(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    admin = _admin(api)
    admin_ids = {i["id"] for i in admin.get(SESSIONS).json()["items"]}
    kid_ids = {i["id"] for i in kid.get(SESSIONS).json()["items"]}
    assert len(admin_ids) == 1 and len(kid_ids) == 1
    for who in (mum, kid):
        seen = {i["id"] for i in who.get(SESSIONS).json()["items"]}
        assert not seen & admin_ids
    victim = next(iter(admin_ids))
    real = _send(api, mum, "DELETE", f"{SESSIONS}/{victim}")
    unknown = _send(api, mum, "DELETE", f"{SESSIONS}/01HZZZZZZZZZZZZZZZZZZZZZZM")
    assert real.status_code == unknown.status_code == 404
    assert real.json()["type"] == unknown.json()["type"]
    assert real.json().get("detail") == unknown.json().get("detail")
    assert admin.get("/api/v1/me").status_code == 200  # still open
    kid_victim = next(iter(kid_ids))
    assert _send(api, mum, "DELETE", f"{SESSIONS}/{kid_victim}").status_code == 404
    assert kid.get("/api/v1/me").status_code == 200
    # "revoke the others" only ever reaches the caller's own sessions
    assert _send(api, mum, "POST", OTHERS).json() == {"revoked": 0}
    assert admin.get("/api/v1/me").status_code == 200 and kid.get("/api/v1/me").status_code == 200


@pytest.mark.req("EF-USR-011", "ENF-SEC-010")
def test_child_lists_and_revokes_only_her_own_sessions(api: AuthEnv) -> None:
    kid, _ = _user(api, "kid", "child", "10.0.0.2", max_age_rating=10)
    second = api.other_client("10.0.0.4")
    assert api.login("kid", client=second).status_code == 200
    items = kid.get(SESSIONS).json()["items"]
    assert len(items) == 2
    assert _send(api, kid, "POST", OTHERS).json() == {"revoked": 1}
    assert second.get("/api/v1/me").status_code == 401
    for path in (ADMIN_SESSIONS, KIT):
        assert kid.get(path).status_code == 403
    victim = next(iter(_admin(api).get(SESSIONS).json()["items"]))["id"]
    assert _send(api, kid, "DELETE", f"{ADMIN_SESSIONS}/{victim}").status_code == 403


@pytest.mark.req("EF-USR-011", "EF-USR-024")
def test_keys_never_list_or_revoke_sessions(api: AuthEnv) -> None:
    """Sessions are an account's own business: a key, whatever its scope, is refused, and so is
    a device token; the admin route is a reserved action that refuses keys too."""
    assert api.login().status_code == 200
    mine = api.client.get(SESSIONS).json()["items"][0]["id"]
    for who in (ADMIN, "kid"):
        if who == "kid":
            _user(api, "kid", "child", "10.0.0.2", max_age_rating=10)
        key = _key(api, who, read_only=who != ADMIN)
        anonymous = api.other_client("10.0.0.50")
        auth = {"Authorization": f"Bearer {key}"}
        for method, path in (
            ("GET", SESSIONS),
            ("POST", OTHERS),
            ("DELETE", f"{SESSIONS}/{mine}"),
            ("GET", ADMIN_SESSIONS),
            ("DELETE", f"{ADMIN_SESSIONS}/{mine}"),
            ("GET", KIT),
        ):
            response = anonymous.request(method, path, headers=auth)
            assert response.status_code in (401, 403), (who, method, path, response.status_code)
    assert api.client.get("/api/v1/me").status_code == 200  # nothing was closed


@pytest.mark.req("EF-USR-011", "EF-USR-024")
def test_admin_session_routes_are_reserved_and_audited(api: AuthEnv) -> None:
    """Listing every profile's sessions and closing one is the admin's: member and child are
    refused (403), a request with no session is refused, the action leaves an audit line."""
    mum, _ = _user(api, "mum", "member", "10.0.0.3")
    admin = _admin(api)
    assert mum.get(ADMIN_SESSIONS).status_code == 403
    assert api.other_client("10.0.0.60").get(ADMIN_SESSIONS).status_code == 401
    everything = admin.get(ADMIN_SESSIONS)
    assert everything.status_code == 200
    names = {i["username"] for i in everything.json()["items"]}
    assert {ADMIN, "mum"} <= names
    mum_session = next(i["id"] for i in everything.json()["items"] if i["username"] == "mum")
    assert _send(api, mum, "DELETE", f"{ADMIN_SESSIONS}/{mum_session}").status_code == 403
    assert mum.get("/api/v1/me").status_code == 200
    closed = _send(api, admin, "DELETE", f"{ADMIN_SESSIONS}/{mum_session}")
    assert closed.status_code == 204
    assert mum.get("/api/v1/me").status_code == 401
    unknown = _send(api, admin, "DELETE", f"{ADMIN_SESSIONS}/01HZZZZZZZZZZZZZZZZZZZZZZM")
    assert unknown.status_code == 404
    lines = db_rows(
        api.settings,
        "SELECT actor_id, session_id FROM audit WHERE action = ?",
        "session.revoke_other",
    )
    assert lines and all(line[1] for line in lines)


@pytest.mark.req("EF-USR-011", "EF-USR-022")
def test_admin_session_revocation_needs_a_fresh_session_when_hardened(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings, hardened=True) as env:
        assert env.login(remember=False).status_code == 200
        mum, _ = _user(env, "mum", "member", "10.0.0.3")
        victim = mum.get(SESSIONS).json()["items"][0]["id"]
        env.clock.advance(timedelta(minutes=16))  # past `fresh_auth_s`, inside the idle window
        stale = _send(env, env.client, "DELETE", f"{ADMIN_SESSIONS}/{victim}")
        assert stale.status_code == 401 and stale.json()["type"].endswith("reauth-required")
        assert mum.get(SESSIONS).status_code == 200  # nothing was closed


@pytest.mark.req("EF-USR-011")
def test_admin_reset_password_cli(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    """The way back in is the host CLI: the password changes, every session of the profile and
    its passkeys close, the others are untouched, and it leaves an audit line without actor."""
    with auth_env(monkeypatch, settings) as env:
        admin = _admin(env)
        mum, _ = _user(env, "mum", "member", "10.0.0.3")
        secret = settings.data_dir.parent / "reset.password"
        secret.write_text(NEW_PASSWORD + "\n", encoding="utf-8")
        secret.chmod(0o600)
    assert (
        run_cli(
            monkeypatch,
            settings,
            "admin",
            "reset-password",
            "--username",
            ADMIN,
            "--password-file",
            str(secret),
        )
        == 0
    )
    with auth_env(monkeypatch, settings, with_admin=False) as env:
        assert env.login(password=PASSWORD).status_code == 401
        assert env.login(password=NEW_PASSWORD).status_code == 200
        assert env.client.get(SESSIONS).json()["items"].__len__() == 1  # the old one is gone
        mum_again = env.other_client("10.0.0.3")
        assert env.login("mum", client=mum_again).status_code == 200  # untouched account
    del admin, mum
    assert db_rows(settings, "SELECT 1 FROM audit WHERE action = ?", "user.password_reset")


@pytest.mark.req("EF-USR-011", "EF-USR-023")
def test_no_web_recovery_code_route(api: AuthEnv) -> None:
    """A child who found a recovery code would walk round the parental barrier: no route creates,
    accepts, lists or mentions one, and `reset-password` is not on the web either."""
    admin = _admin(api)
    paths = admin.get("/api/v1/openapi.json").json()["paths"]
    for path in paths:
        lowered = path.lower()
        assert "recover" not in lowered.replace("recovery-kit", ""), path
        assert "reset" not in lowered, path
        assert "forgot" not in lowered, path
    assert set(paths[KIT]) == {"get"}  # the kit is read, never posted to
    for path in (
        "/api/v1/auth/recovery", "/api/v1/auth/recovery-code", "/api/v1/auth/reset-password",
        "/api/v1/admin/reset-password", "/api/v1/auth/forgot", "/api/v1/auth/password/reset",
        "/api/v1/recovery-kit", "/api/v1/admin/recovery-code", "/api/v1/admin/recovery",
    ):  # fmt: skip
        for method in ("GET", "POST", "PUT"):
            response = admin.request(method, path, json={}, headers={CSRF_HEADER: api.csrf(admin)})
            assert response.status_code in (404, 405), (method, path)


def _keyring(env: AuthEnv) -> list[bytes]:
    """The material of every key of the keyring of this instance, created for the test."""
    keyring = diagnostics.boot_check(env.settings, announce=False)
    return [key.material for key in keyring.keys]


def _forms(material: bytes) -> list[str | bytes]:
    return [
        material,
        material.hex(),
        base64.b64encode(material).decode(),
        base64.urlsafe_b64encode(material).decode(),
        base64.b64encode(material).decode().rstrip("="),
        base64.b32encode(material).decode(),
    ]


@pytest.mark.req("EF-USR-023", "EF-USR-011")
def test_recovery_kit_contains_no_secret(api: AuthEnv) -> None:
    """Search the bytes: no key of the keyring in any encoding, no password, no passphrase, no
    session or CSRF value, no API key; it says where the passphrase is and holds none."""
    materials = _keyring(api)
    admin = _admin(api)
    key = _key(api, ADMIN)
    passphrase_file = api.settings.data_dir / "export.pass"
    passphrase_file.write_text("a very secret export passphrase", encoding="utf-8")
    response = admin.get(KIT)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "no-store" in response.headers["cache-control"]
    assert "script-src" not in response.headers.get("content-security-policy", "").replace(
        "script-src 'none'", ""
    )
    page = response.content
    for material in materials:
        for form in _forms(material):
            needle = form if isinstance(form, bytes) else form.encode()
            assert needle not in page
    cookie = admin.cookies.get(SESSION_COOKIE)
    assert cookie and cookie.encode() not in page
    for secret in (
        PASSWORD,
        NEW_PASSWORD,
        key,
        "a very secret export passphrase",
        admin.cookies.get(CSRF_COOKIE) or "-",
    ):
        assert secret.encode() not in page, secret
    text = response.text
    assert "shelfhold admin reset-password" in text
    assert "<script" not in text.lower() and "javascript:" not in text.lower()
    assert "http://" not in text and "https://" not in text.replace("https://example", "")


@pytest.mark.req("EF-USR-023", "EF-BIB-054")
def test_recovery_kit_references_keyring_export_without_key(api: AuthEnv) -> None:
    from shelfhold.identity.public import KEYRING_EXPORT_NAME

    materials = _keyring(api)
    text = _admin(api).get(KIT).text
    assert KEYRING_EXPORT_NAME in text  # the file is named, in a separate place from the passphrase
    assert "secrets export" in text and "rotation" in text.lower()  # redone after every rotation
    assert "Kavita" in text  # reading without Shelfhold
    assert "passphrase" in text.lower() or "phrase de passe" in text.lower()
    for material in materials:
        assert material.hex() not in text and base64.b64encode(material).decode() not in text


@pytest.mark.req("EF-USR-023")
def test_recovery_kit_is_for_the_admin_session_only(api: AuthEnv) -> None:
    mum, _ = _user(api, "mum", "member", "10.0.0.3")
    assert mum.get(KIT).status_code == 403
    assert api.other_client("10.0.0.61").get(KIT).status_code == 401
    assert _admin(api).get(KIT).status_code == 200
