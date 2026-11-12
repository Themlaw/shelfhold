"""EF-INT-023 (passe 13, N2): reserved exit commands, played through the real route of each."""

import re
from collections.abc import Callable, Iterator
from datetime import timedelta
from types import SimpleNamespace

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import (
    EXIT_ROUTES,
    Allowed,
    Credential,
    ExitRoute,
    Principal,
    authorize_reserved,
)
from shelfhold.ops import ENDPOINTS
from tests.auth_env import AuthEnv, auth_env, run_cli

RESERVED_ROUTES = [r for r in EXIT_ROUTES if r.reserved is not None]
SERVED = [r for r in RESERVED_ROUTES if r.command in ENDPOINTS]  # a route of `create_app()`
HOST_ONLY = {"restore acknowledge"}  # the host CLI only: a session, however fresh, is refused
SESSION_SERVED = [r for r in SERVED if r.command not in HOST_ONLY]
KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
BODIES: dict[str, dict[str, object]] = {
    "target rotate-key": {"password": "x"},
    "source set-secret": {"secret": "user:pass"},
    "target recheck [child_exposure]": {},
    "reconcile --adopt-after-restore [confirm-deletion]": {"confirm_deletion": True},
    "reconcile --forget-root": {"root_id": "r", "confirm": "x"},
    "tombstone accept-lost-keyring": {"confirmation": "x"},
    "tombstone set-aside-journal": {},
    "secrets import": {"passphrase_file": "p", "in_file": "export.age"},
    "rate": {"age": "13+"},
    "root takeover": {},
    "root reinit": {},
    "store reinit": {},
    "restore acknowledge": {},
    "maintenance off": {},
}


def _path(route: ExitRoute) -> str:
    return re.sub(r"\{[a-z_]+\}", "x", ENDPOINTS[route.command][1])


class _Ops:
    """Records which operation a route reached; every one succeeds."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rating = SimpleNamespace(rate=self._rate)
        self.maintenance = SimpleNamespace(leave=self._leave)

    def _leave(self, **_: object) -> dict[str, object]:
        self.calls.append("maintenance_leave")
        return {"active": False, "changed": True}

    def _rate(self, age: str, **_: object) -> dict[str, object]:
        self.calls.append("rate")
        return {
            "target": "work:x", "label": "age.min.13", "effective": 13, "effective_x": False,
            "withdrawals": [],
        }  # fmt: skip

    def target_recheck_is_reserved(self, _target: str) -> bool:
        return True  # the conditional exit under test

    def target_recheck(self, _target: str) -> SimpleNamespace:
        self.calls.append("target_recheck")
        return SimpleNamespace(cleared=(), denied=())

    def tombstone_accept_lost_keyring(self, **_: object) -> list[str]:
        self.calls.append("tombstone_accept_lost_keyring")
        return []

    def restore_acknowledge(self, **_: object) -> list[str]:
        self.calls.append("restore_acknowledge")
        return []

    def secrets_import(self, **_: object) -> dict[str, object]:
        self.calls.append("secrets_import")
        return {"added": [], "present": 0, "replayed": 0}

    def _root(self, name: str) -> dict[str, object]:
        self.calls.append(name)
        return {
            "command": name, "root_id": "x", "path": "/x", "previous": "foreign_root",
            "sentinel": "restamped", "predecessors": [], "missing": 0, "planned": 0,
            "units_store_missing": 0, "distrusted_copies": 0, "ignored_claims": [], "notes": [],
        }  # fmt: skip

    def root_takeover(self, *_a: object, **_k: object) -> dict[str, object]:
        return self._root("root_takeover")

    def root_reinit(self, *_a: object, **_k: object) -> dict[str, object]:
        return self._root("root_reinit")

    def store_reinit(self, *_a: object, **_k: object) -> dict[str, object]:
        return self._root("store_reinit")

    def tombstone_set_aside_journal(self, **_: object) -> dict[str, str]:
        self.calls.append("tombstone_set_aside_journal")
        return {}

    @staticmethod
    def maintenance_active() -> bool:
        return False  # asked by the gate of the socket methods, not an operation of a route

    def __getattr__(self, name: str) -> Callable[..., None]:
        def operation(*_a: object, **_k: object) -> None:
            self.calls.append(name)

        return operation


@pytest.fixture
def ops() -> _Ops:
    return _Ops()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        yield env


@pytest.mark.req("EF-INT-023", "EF-USR-024")
def test_reserved_exit_commands_require_fresh_session_or_cli(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    ops: _Ops,
) -> None:
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "ci") == 0
    key = KEY.search(capsys.readouterr().out)
    assert key
    bearer = {"Authorization": f"Bearer {key.group(0)}"}
    api.login()
    by_name = {r.reserved.name: r.reserved for r in RESERVED_ROUTES if r.reserved}
    assert SERVED, "no reserved exit is served"
    # fresh session: every served reserved exit goes through
    for route in SESSION_SERVED:
        assert api.post(_path(route), json=BODIES[route.command]).status_code == 200, route
    for route in SERVED:
        if route.command in HOST_ONLY:  # never over HTTP, a fresh session included
            denied = api.post(_path(route), json=BODIES[route.command])
            assert denied.status_code == 403, route
            assert denied.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert len(ops.calls) == len(SESSION_SERVED)
    ops.calls.clear()
    # an API key, admin scope included, never does
    for route in SERVED:
        refused = api.client.post(_path(route), json=BODIES[route.command], headers=bearer)
        assert refused.status_code == 403, route
        assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert ops.calls == []
    # a stale session needs re-authentication exactly where the property says so
    api.clock.advance(timedelta(minutes=20))
    for route in SESSION_SERVED:
        assert route.reserved is not None
        stale = api.post(_path(route), json=BODIES[route.command])
        wants_fresh = route.reserved.needs_fresh_auth(
            restricted_profile_exists=False, hardened=False
        )
        assert stale.status_code == (401 if wants_fresh else 200), route
    # the host CLI is allowed on all of them
    cli = Principal(
        user_id="-", username="host", role="admin", credential=Credential.LOCAL_CLI, can_rate=True
    )
    for action in by_name.values():
        verdict = authorize_reserved(cli, action, now=api.clock.now())
        assert isinstance(verdict, Allowed)
