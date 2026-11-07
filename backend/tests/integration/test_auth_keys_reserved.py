"""API keys, reserved actions and the minimal `fresh_auth_s` (EF-USR-003, EF-USR-022, EF-USR-024).

The routes are the real ones of `create_app()`: `purge --now` (a reserved action that wants a
fresh session), the recheck of a target (reserved only for a child exposure, no freshness) and the
plan preview (open to a key with the `admin` scope). The services behind them are recorded.
"""

import hashlib
import re
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.routing import APIRoute

from shelfhold.config import Settings
from shelfhold.identity.public import Principal, ReservedAction
from tests.auth_env import (
    PASSWORD,
    SESSION_COOKIE,
    WRONG,
    AuthEnv,
    auth_env,
    db_dump,
    db_rows,
    run_cli,
)

KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")
PURGE = "/api/v1/units/u1/purge"
RECHECK = "/api/v1/targets/t1/recheck"
PLANS = "/api/v1/plans"
PLAN_BODY = {"shelf_destination_id": "d1"}


class _Library:
    def __init__(self, effects: list[str]) -> None:
        self._effects = effects

    def purge(self, *, principal: Principal, **_: object) -> dict[str, str]:
        self._effects.append(f"purged-by-{principal.credential}")
        return {"ok": "purged"}


class _Services:
    """The operating and plan services behind the routes, recording what reached them."""

    def __init__(self, effects: list[str]) -> None:
        self.effects = effects
        self.library = _Library(effects)

    def target_recheck_is_reserved(self, _target: str) -> bool:
        return True  # a child exposure: the reserved case, with no freshness requirement

    def target_recheck(self, _target: str) -> SimpleNamespace:
        self.effects.append("key")
        return SimpleNamespace(cleared=(), denied=())

    def preview(self, _destination: str) -> SimpleNamespace:
        self.effects.append(f"thing-{len(self.effects)}")
        return SimpleNamespace(
            plan_id=f"plan-{len(self.effects)}", base_version="v1", ops=[], blocked=[],
            warnings=[], checks=(),
        )  # fmt: skip


@pytest.fixture
def effects() -> list[str]:
    return []


@pytest.fixture
def api(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, effects: list[str]
) -> Iterator[AuthEnv]:
    services = _Services(effects)
    with auth_env(monkeypatch, settings, ops_services=services, plan_service=services) as env:
        yield env


def _create_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    *extra: str,
) -> str:
    capsys.readouterr()
    assert (
        run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "scripts", *extra) == 0
    )
    out = capsys.readouterr().out
    match = KEY.search(out)
    assert match, out
    return match.group(0)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.mark.req("EF-USR-003")
def test_cli_admin_create_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    key = _create_key(monkeypatch, capsys, api.settings)
    assert api.client.get("/api/v1/about", headers=_bearer(key)).status_code == 200
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "list") == 0
    listing = capsys.readouterr().out
    prefix = KEY.fullmatch(key).group(1)  # type: ignore[union-attr]
    assert prefix in listing
    assert key not in listing


@pytest.mark.req("EF-USR-003")
def test_api_key_stored_hashed_prefix_shown_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    key = _create_key(monkeypatch, capsys, api.settings)
    secret = key.split("_", 2)[2]
    dump = db_dump(api.settings)
    assert key not in dump
    assert secret not in dump
    [(prefix, digest, scopes)] = db_rows(api.settings, "SELECT prefix, hash, scopes FROM api_key")
    assert key.startswith(f"shk_{prefix}_")
    assert digest == "sha256:" + hashlib.sha256(key.encode()).hexdigest()
    assert scopes == '["admin"]'


@pytest.mark.req("EF-USR-003")
def test_revoked_key_gets_401_next_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    key = _create_key(monkeypatch, capsys, api.settings)
    assert api.client.get("/api/v1/about", headers=_bearer(key)).status_code == 200
    prefix = KEY.fullmatch(key).group(1)  # type: ignore[union-attr]
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "revoke", prefix) == 0
    assert api.client.get("/api/v1/about", headers=_bearer(key)).status_code == 401
    # A forged key with a valid prefix but another secret is refused too.
    forged = f"shk_{prefix}_" + "A" * 43
    assert api.client.get("/api/v1/about", headers=_bearer(forged)).status_code == 401


@pytest.mark.req("EF-USR-003")
def test_expired_key_rejected(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    key = _create_key(monkeypatch, capsys, api.settings, "--expires-days", "1")
    # The CLI takes the system clock; move the app's clock past it.
    [(expires_at,)] = db_rows(api.settings, "SELECT expires_at FROM api_key")
    assert expires_at is not None
    from datetime import UTC, datetime

    api.clock.current = datetime.now(UTC) + timedelta(days=2)
    assert api.client.get("/api/v1/about", headers=_bearer(key)).status_code == 401


@pytest.mark.req("EF-USR-003")
def test_key_last_used_and_audit_entries(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    key = _create_key(monkeypatch, capsys, api.settings)
    assert db_rows(api.settings, "SELECT last_used_at FROM api_key") == [(None,)]
    api.client.get("/api/v1/about", headers=_bearer(key))
    [(last_used,)] = db_rows(api.settings, "SELECT last_used_at FROM api_key")
    assert last_used is not None
    prefix = KEY.fullmatch(key).group(1)  # type: ignore[union-attr]
    run_cli(monkeypatch, api.settings, "admin", "keys", "revoke", prefix)
    actions = [a for (a,) in db_rows(api.settings, "SELECT action FROM audit ORDER BY at")]
    assert "api_key.created" in actions
    assert "api_key.revoked" in actions


@pytest.mark.req("EF-USR-003", "EF-USR-024")
def test_admin_api_key_expiry_optional_unless_hardened(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        _create_key(monkeypatch, capsys, env.settings)
        assert db_rows(env.settings, "SELECT expires_at FROM api_key") == [(None,)]
    hardened = settings.model_copy(update={"auth_hardened": True})
    _create_key(monkeypatch, capsys, hardened)
    _create_key(monkeypatch, capsys, hardened, "--expires-days", "365")
    rows = db_rows(
        hardened,
        "SELECT julianday(expires_at) - julianday(created_at) FROM api_key "
        "WHERE expires_at IS NOT NULL",
    )
    assert len(rows) == 2
    assert all(89.9 < float(days) <= 90.01 for (days,) in rows)  # type: ignore[arg-type]


@pytest.mark.req("EF-USR-003")
def test_read_only_key_post_403_and_keys_need_no_csrf(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    effects: list[str],
) -> None:
    read_only = _create_key(monkeypatch, capsys, api.settings, "--read-only")
    refused = api.client.post(PLANS, json=PLAN_BODY, headers=_bearer(read_only))
    assert refused.status_code == 403
    assert refused.json()["code"] == "forbidden"
    assert api.client.get("/api/v1/about", headers=_bearer(read_only)).status_code == 200
    writer = _create_key(monkeypatch, capsys, api.settings)
    assert api.client.post(PLANS, json=PLAN_BODY, headers=_bearer(writer)).status_code == 201
    assert effects == ["thing-0"]


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_api_key_admin_cannot_purge_now(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    effects: list[str],
) -> None:
    key = _create_key(monkeypatch, capsys, api.settings)
    for path in (PURGE, RECHECK):
        refused = api.client.post(path, json={"typed": "x"}, headers=_bearer(key))
        assert refused.status_code == 403
        assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert effects == []


@pytest.mark.req("EF-USR-022", "EF-USR-024")
def test_reserved_action_requires_fresh_auth_from_m1a(api: AuthEnv, effects: list[str]) -> None:
    api.login()
    assert api.post(PURGE, json={"typed": "x"}).status_code == 200  # just logged in: fresh
    api.clock.advance(timedelta(minutes=20))
    stale = api.post(PURGE, json={"typed": "x"})
    assert stale.status_code == 401
    body = stale.json()
    assert body["type"] == "/api/v1/problems/reauth-required"
    assert body["auth_age_s"] == 1200
    assert body["max_age_s"] == 900
    assert body["reauth_method"] == "password"
    # A reserved action without freshness by default (P3) goes through on a plain session.
    assert api.post(RECHECK).status_code == 200
    old_token = api.client.cookies[SESSION_COOKIE]
    wrong = api.post("/api/v1/auth/reauth", json={"method": "password", "password": "nope-nope"})
    assert wrong.status_code == 401
    again = api.post("/api/v1/auth/reauth", json={"method": "password", "password": PASSWORD})
    assert again.status_code == 200
    assert api.client.cookies[SESSION_COOKIE] != old_token  # renewed at re-authentication
    assert api.post(PURGE, json={"typed": "x"}).status_code == 200
    assert effects == ["purged-by-session", "key", "purged-by-session"]
    rows = db_rows(
        api.settings,
        "SELECT session_id, auth_time FROM audit WHERE action = 'purge.now'",
    )
    assert len(rows) == 2
    assert all(session_id and auth_time for session_id, auth_time in rows)


@pytest.mark.req("EF-USR-022")
def test_reauth_unknown_method_refused(api: AuthEnv) -> None:
    api.login()
    response = api.post("/api/v1/auth/reauth", json={"method": "passkey", "assertion": "x"})
    assert response.status_code == 422


@pytest.mark.req("EF-USR-022", "EF-USR-024")
def test_reauth_problem_type_in_openapi(api: AuthEnv) -> None:
    schema = api.app.openapi()  # type: ignore[attr-defined]
    types = schema["x-problem-types"]
    assert types["/api/v1/problems/reauth-required"]["status"] == 401
    # L04: the two ages are part of the type, and nothing else of the session
    fields = types["/api/v1/problems/reauth-required"]["extensions"]
    assert set(fields) == {"auth_age_s", "max_age_s"}
    assert all(spec == {"type": "integer"} for spec in fields.values())
    body = schema["components"]["schemas"]["ReauthRequiredProblem"]
    assert {"auth_age_s", "max_age_s"} <= set(body["required"])
    assert types["/api/v1/problems/credential-not-allowed"]["status"] == 403
    purge = schema["paths"]["/api/v1/units/{unit_id}/purge"]["post"]
    assert {"401", "403"} <= set(purge["responses"])


@pytest.mark.req("EF-INT-001")
def test_idempotency_keys_are_scoped_to_the_authenticated_actor(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    effects: list[str],
) -> None:
    key = _create_key(monkeypatch, capsys, api.settings)
    api.login()
    same = {"Idempotency-Key": "same-key-123"}
    first = api.post(PLANS, json=PLAN_BODY, headers=same)
    replay = api.post(PLANS, json=PLAN_BODY, headers=same)
    assert replay.headers.get("idempotent-replayed") == "true"
    assert replay.json() == first.json()
    by_key = api.client.post(PLANS, json=PLAN_BODY, headers={**same, **_bearer(key)})
    assert "idempotent-replayed" not in by_key.headers
    assert by_key.json()["plan_id"] != first.json()["plan_id"]
    assert len(effects) == 2
    # A login is never stored nor replayed: it carries a fresh secret every time.
    logins = [
        api.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": PASSWORD},
            headers={"Idempotency-Key": "login-1"},
        )
        for _ in range(2)
    ]
    assert all("idempotent-replayed" not in r.headers for r in logins)
    assert logins[0].json()["csrf_token"] != logins[1].json()["csrf_token"]


def _reserved_routes(app: object) -> list[tuple[str, str, str]]:
    """(method, path, declared action name) of every route of the real app guarded by a `reserved_*`
    dependency, found in the route table and not listed by hand."""
    found: list[tuple[str, str, str]] = []

    def names(dependant: object) -> list[str]:
        out: list[str] = []
        for sub in dependant.dependencies:  # type: ignore[attr-defined]
            declared = getattr(sub.call, "reserved_action", None)  # the action, not a name prefix
            if isinstance(declared, ReservedAction):
                out.append(declared.name)
            out.extend(names(sub))
        return out

    for route in app.routes:  # type: ignore[attr-defined]
        # an included router is lazy in this FastAPI: its contexts carry the final path
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            contexts = contexts()
        for ctx in contexts if contexts is not None else [route]:
            if not isinstance(getattr(ctx, "original_route", ctx), APIRoute):
                continue
            for name in names(ctx.dependant):
                for method in sorted(ctx.methods - {"HEAD", "OPTIONS"}):
                    found.append((method, ctx.path, name))
    return found


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_reserved_routes_of_create_app_refuse_api_keys(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        routes = _reserved_routes(env.app)
        guards = {name for _method, _path, name in routes}
        # the routes of production, not throwaway ones: purge, key rotation, secrets, ceilings...
        assert {"purge.now", "admin_risk.accept", "request.unmask"} <= guards
        assert len(routes) >= 6
        key = _create_key(monkeypatch, capsys, settings)
        for method, path, name in routes:
            url = re.sub(r"\{[^}]+\}", "x", path)
            refused = env.client.request(method, url, headers=_bearer(key), json={})
            assert refused.status_code == 403, (method, path, name, refused.text)
            assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed", path
            anonymous = env.client.request(method, url, json={})
            assert anonymous.status_code in {401, 403}, (method, path)


@pytest.mark.req("EF-USR-022")
def test_hardened_profile_refuses_remember_without_confirmation(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings, hardened=True) as env:
        refused = env.login()  # "remember me" is ticked by default: the admin must confirm it
        assert refused.status_code == 422
        assert refused.json()["type"] == "/api/v1/problems/remember-needs-confirmation"
        assert SESSION_COOKIE not in env.client.cookies
        assert db_rows(env.settings, "SELECT count(*) FROM session") == [(0,)]
        confirmed = env.login(confirm_remember=True)
        assert confirmed.status_code == 200
        assert "max-age" in confirmed.headers["set-cookie"].lower()
        assert db_rows(env.settings, "SELECT remember FROM session") == [(1,)]
        plain = env.login(remember=False)  # a browser session needs no confirmation
        assert plain.status_code == 200
        assert db_rows(env.settings, "SELECT remember FROM session ORDER BY created_at") == [
            (1,),
            (0,),
        ]
        wrong = env.login(password=WRONG, confirm_remember=True)
        assert wrong.status_code == 401  # a wrong password is never told apart by the profile


@pytest.mark.req("EF-USR-022")
def test_default_profile_keeps_remember_me_without_confirmation(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200  # P2: the default stays ticked, no extra step
        assert db_rows(env.settings, "SELECT remember FROM session") == [(1,)]
