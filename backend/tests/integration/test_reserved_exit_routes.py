"""EF-INT-023 (passe 13, N2): reserved exit commands, played through a throwaway route each."""

import re
from collections.abc import Iterator
from datetime import timedelta

import pytest
from fastapi import APIRouter, Depends

from shelfhold.config import Settings
from shelfhold.identity.public import (
    EXIT_ROUTES,
    Allowed,
    Credential,
    ExitRoute,
    Principal,
    authorize_reserved,
)
from shelfhold.interfaces.http_api.auth import reserved_action
from tests.auth_env import AuthEnv, auth_env, run_cli

RESERVED_ROUTES = [r for r in EXIT_ROUTES if r.reserved is not None]
KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")


def _path(route: ExitRoute) -> str:
    assert route.reserved is not None
    return "/api/v1/test/exit/" + route.reserved.name.replace(".", "-").replace("_", "-")


def _router(calls: list[str]) -> APIRouter:
    router = APIRouter()
    for route in RESERVED_ROUTES:
        assert route.reserved is not None
        action = route.reserved

        def handler(
            principal: Principal = Depends(reserved_action(action)),  # noqa: B008
            _name: str = action.name,
        ) -> dict[str, str]:
            calls.append(_name)
            return {"ok": _name}

        router.add_api_route(_path(route), handler, methods=["POST"], name=f"exit_{action.name}")
    return router


@pytest.fixture
def calls() -> list[str]:
    return []


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings, calls: list[str]) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, routers=[_router(calls)]) as env:
        yield env


@pytest.mark.req("EF-INT-023", "EF-USR-024")
def test_reserved_exit_commands_require_fresh_session_or_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    calls: list[str],
) -> None:
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "ci") == 0
    key = KEY.search(capsys.readouterr().out)
    assert key
    bearer = {"Authorization": f"Bearer {key.group(0)}"}
    api.login()
    by_name = {r.reserved.name: r.reserved for r in RESERVED_ROUTES if r.reserved}
    # fresh session: every reserved exit goes through
    for route in RESERVED_ROUTES:
        assert api.post(_path(route)).status_code == 200, route
    assert sorted(set(calls)) == sorted(by_name)
    calls.clear()
    # an API key, admin scope included, never does
    for route in RESERVED_ROUTES:
        refused = api.client.post(_path(route), headers=bearer)
        assert refused.status_code == 403, route
        assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert calls == []
    # a stale session needs re-authentication exactly where the property says so
    api.clock.advance(timedelta(minutes=20))
    for route in RESERVED_ROUTES:
        assert route.reserved is not None
        stale = api.post(_path(route))
        wants_fresh = route.reserved.needs_fresh_auth(
            restricted_profile_exists=False, hardened=False
        )
        assert stale.status_code == (401 if wants_fresh else 200), route
    # the host CLI is allowed on all of them
    cli = Principal(user_id="-", username="host", role="admin", credential=Credential.LOCAL_CLI)
    for action in by_name.values():
        verdict = authorize_reserved(cli, action, now=api.clock.now())
        assert isinstance(verdict, Allowed)
