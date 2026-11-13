"""Review of PR #41 (L05): each finding of the adversarial review is a regression test here."""

import re
import threading
from datetime import timedelta
from typing import Any

import pytest
from fastapi import APIRouter, Depends, Request

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.http_api.auth import require_principal, reserved_action
from tests.auth_env import (
    ADMIN,
    CSRF_COOKIE,
    SESSION_COOKIE,
    WRONG,
    auth_env,
    db_rows,
    run_cli,
)

EXPORT = RESERVED_ACTIONS["secrets.export"]  # a declared action: an undeclared one is refused


def _router(calls: list[str]) -> APIRouter:
    router = APIRouter(prefix="/api/v1/review")

    @router.post("/export")
    def export(p: Principal = Depends(reserved_action(EXPORT))) -> dict[str, str]:  # noqa: B008
        calls.append(p.session_id or "?")
        return {"secret": "KEYRING-EXPORT-42"}

    @router.get("/headers")
    def headers(request: Request) -> dict[str, list[str]]:
        return {"names": sorted(request.headers.keys())}

    @router.post("/thing")
    def thing(p: Principal = Depends(require_principal)) -> dict[str, str]:  # noqa: B008
        return {"actor": p.actor_id}

    return router


IDENTITY = ("Tailscale-User-Login", "Remote-User", "remote_user", "X-Forwarded-User")


# --- Blocking 1: identity headers are never believed, never rejected --------------------------


@pytest.mark.req("ENF-SEC-008")
def test_tunnel_identity_header_without_forward_auth_rejected(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    """P5 as decided at review: "refused" = never believed. `tailscale serve` keeps working."""
    with auth_env(monkeypatch, settings, routers=[_router([])]) as env:
        for header in IDENTITY:
            seen = env.client.get("/api/v1/review/headers", headers={header: ADMIN})
            assert seen.status_code == 200
            names = seen.json()["names"]
            assert header.lower().replace("_", "-") not in names, names
            assert not [n for n in names if n.startswith(("remote", "tailscale", "x-forwarded"))]
            anonymous = env.client.get("/api/v1/about", headers={header: ADMIN})
            assert anonymous.status_code == 401
            assert anonymous.json()["code"] == "unauthenticated"
            assert env.client.get("/login", headers={header: ADMIN}).status_code == 200
            assert env.client.get("/healthz", headers={header: ADMIN}).status_code == 200
        env.login()
        tunnel = env.client.get("/api/v1/about", headers={"Tailscale-User-Login": "x"})
        assert tunnel.status_code == 200
    rows = db_rows(
        settings,
        "SELECT target, count(*) FROM audit WHERE action = 'auth.identity_header_stripped' "
        "GROUP BY target",
    )
    assert {target for target, _ in rows} >= {"tailscale-user-login", "remote-user"}
    assert all(count == 1 for _, count in rows)  # once per header name
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "status") == 0
    assert "tailscale-user-login" in capsys.readouterr().out


@pytest.mark.req("ENF-SEC-008")
def test_identity_headers_never_reach_the_routes_even_from_the_forward_auth_networks(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """L06: a believed proxy hands the name on through the authentication, not as a header."""
    configured = settings.model_copy(
        update={"auth_forward_enabled": True, "http_trusted_proxies": ("10.0.0.0/8",)}
    )
    with auth_env(monkeypatch, configured, routers=[_router([])]) as env:
        for ip in ("10.1.2.3", "192.0.2.9"):
            client = env.other_client(ip)
            seen = client.get("/api/v1/review/headers", headers={"Remote-User": ADMIN})
            assert "remote-user" not in seen.json()["names"]


# --- Blocking 2: idempotent replay must not bypass the reserved-action checks -----------------


@pytest.mark.req("EF-USR-022", "EF-USR-024", "EF-INT-001")
def test_idempotent_replay_never_skips_fresh_auth(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    calls: list[str] = []
    with auth_env(monkeypatch, settings, routers=[_router(calls)]) as env:
        stale = env.other_client("198.51.100.9")
        env.login(client=stale)
        env.login()
        key = {"Idempotency-Key": "review-key-1"}
        first = env.post("/api/v1/review/export", json={"x": 1}, headers=key)
        assert first.status_code == 200
        again = env.post("/api/v1/review/export", json={"x": 1}, headers=key)
        assert "idempotent-replayed" not in again.headers  # never stored: the checks ran again
        assert len(calls) == 2
        env.clock.advance(timedelta(minutes=20))
        for client in (stale, env.client):
            replay = env.post("/api/v1/review/export", json={"x": 1}, headers=key, client=client)
            assert replay.status_code == 401
            assert replay.json()["code"] == "reauth_required"
            assert "KEYRING" not in replay.text
        assert len(calls) == 2
    assert db_rows(settings, "SELECT count(*) FROM idempotency_key") == [(0,)]


@pytest.mark.req("EF-INT-001")
def test_idempotency_actor_is_one_credential(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings, routers=[_router([])]) as env:
        other = env.other_client("198.51.100.10")
        env.login(client=other)
        env.login()
        mine = env.post("/api/v1/review/thing").json()["actor"]
        theirs = env.post("/api/v1/review/thing", client=other).json()["actor"]
    assert re.fullmatch(r"session:[0-9A-Z]{26}", mine)
    assert mine != theirs  # two sessions of one account share no key


# --- Minor 3 and 4: attempt limiting -----------------------------------------------------------


@pytest.mark.req("ENF-SEC-008")
def test_concurrent_attempts_cannot_slip_under_the_limit(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        attacker = env.other_client("203.0.113.66")
        codes: list[int] = []
        lock = threading.Lock()
        barrier = threading.Barrier(30)

        def attempt() -> None:
            barrier.wait()
            response = attacker.post(
                "/api/v1/auth/login", json={"username": "nobody", "password": WRONG}
            )
            with lock:
                codes.append(response.status_code)

        threads = [threading.Thread(target=attempt) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert codes.count(401) == 10
    assert codes.count(429) == 20


@pytest.mark.req("ENF-SEC-008")
def test_success_withdraws_its_own_attempt_only(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        for _ in range(9):
            env.login(password=WRONG)
        assert env.login().status_code == 200  # the 10th attempt, right: withdrawn
        assert env.login(password=WRONG).status_code == 401  # 10th failure
        assert env.login(password=WRONG).status_code == 429


@pytest.mark.req("ENF-SEC-008")
def test_known_browser_escapes_third_party_account_lockout(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        owner = env.other_client("198.51.100.7")
        assert env.login(client=owner).status_code == 200  # gets the signed device cookie
        assert "__Host-dev" in owner.cookies
        for n in range(3):
            attacker = env.other_client(f"203.0.113.{n + 1}")
            for _ in range(10):
                env.login("Admin", WRONG, client=attacker)
        stranger = env.other_client("198.51.100.8")
        assert env.login(client=stranger).status_code == 429  # account backoff applies
        assert env.login(client=owner).status_code == 200  # known browser: per-account skipped
        forged = env.other_client("198.51.100.11")
        forged.cookies.set("__Host-dev", "e30.forged", domain="testserver.local")
        assert env.login(client=forged).status_code == 429


# --- Minor 5: no forwarded headers ------------------------------------------------------------


@pytest.mark.req("ENF-SEC-008")
def test_serve_never_lets_uvicorn_rewrite_the_peer(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    import uvicorn

    captured: dict[str, Any] = {}
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: captured.update(k))
    monkeypatch.setattr(
        "shelfhold.interfaces.cli.serve.boot_check", lambda *_a, **_k: None, raising=True
    )
    from shelfhold.infrastructure.migrate import migrate

    migrate(settings)
    assert run_cli(monkeypatch, settings, "serve") == 0
    assert captured["proxy_headers"] is False
    assert captured["forwarded_allow_ips"] == ""


@pytest.mark.req("ENF-SEC-008")
def test_forwarded_for_does_not_change_the_throttled_address(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        for _ in range(10):
            env.login(password=WRONG)
        moved = env.client.post(
            "/api/v1/auth/login",
            json={"username": ADMIN, "password": WRONG},
            headers={"X-Forwarded-For": "9.9.9.9"},
        )
        assert moved.status_code == 429  # no trusted proxy: the header is only a claim


# --- Minor 6: CSRF cookie slides with the session ---------------------------------------------


@pytest.mark.req("EF-USR-021", "EF-USR-022")
def test_csrf_cookie_slides_with_the_session(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        env.login()
        env.clock.advance(timedelta(days=29))
        touched = env.client.get("/api/v1/auth/session")
        lines = touched.headers.get_list("set-cookie")
        assert any(line.startswith(f"{SESSION_COOKIE}=") for line in lines)
        csrf = [line for line in lines if line.startswith(f"{CSRF_COOKIE}=")]
        assert csrf
        assert "max-age=2592000" in csrf[0].lower()
        env.clock.advance(timedelta(days=29))  # past the first cookie's 30 days
        assert env.post("/api/v1/auth/logout").status_code == 204
