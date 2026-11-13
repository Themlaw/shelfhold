"""M3 L26 (EF-BIB-030, EF-INT-014, EF-USR-024): the doors of the reclaim actions, through the real
application: one HTTP door and one CLI door per action, both reaching the same service method.

The services behind the routes are recorded (as in `test_auth_keys_reserved`): what is tried here
is who may reach them, with which word, and that no second path exists.
"""

from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS, Principal
from shelfhold.interfaces.cli.registry import build_parser
from shelfhold.interfaces.local_cli.ops import OPS_METHODS
from shelfhold.library.application.disk_guard import GIB, check_add_fits
from tests.auth_env import AuthEnv, auth_env, db_dump, db_rows, run_cli
from tests.integration.test_reserved_real_path import add_user, make_key, real_reserved_routes

pytestmark = pytest.mark.req("EF-BIB-030", "EF-USR-024")

EMPTY = "/api/v1/trash/empty"
LIFT = "/api/v1/tombstones/7/lift"
PURGE = "/api/v1/units/u1/purge"
SPACE = "/api/v1/library/space-check"
PHRASE = "empty-trash"


class _Library:
    def __init__(self, effects: list[str]) -> None:
        self.effects = effects

    def trash_empty(self, *, typed: str | None, principal: Principal) -> dict[str, Any]:
        self.effects.append(f"empty:{typed}:{principal.credential}")
        return {"removed": 2, "kept": 0, "held_irreplaceable": 0, "held_other": 0}

    def purge(self, *, principal: Principal, typed: str | None, **_: object) -> dict[str, Any]:
        self.effects.append(f"purge:{typed}:{principal.credential}")
        return {
            "anchor_id": "u1", "units": ["u1"], "complete": True, "erased": 1,
            "unreachable_roots": [], "pending_roots": [], "alerts": [], "out_of_reach": [],
        }  # fmt: skip

    def space_check(self, *, estimated_bytes: int) -> dict[str, Any]:
        figures = check_add_fits(estimated_bytes, free_bytes=40 * GIB, reserve_bytes=10 * GIB)
        return {"fits": True, **figures.as_dict()}


class _Services:
    def __init__(self, effects: list[str]) -> None:
        self.effects = effects
        self.library = _Library(effects)

    def tombstone_lift(self, *, seq: int, typed: str, principal: Principal) -> dict[str, Any]:
        self.effects.append(f"lift:{seq}:{typed}:{principal.credential}")
        return {"seq": seq}


@pytest.fixture
def effects() -> list[str]:
    return []


@pytest.fixture
def api(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, effects: list[str]
) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=_Services(effects)) as env:
        yield env


def test_empty_trash_over_http_is_a_reserved_action_with_a_typed_word(
    api: AuthEnv, effects: list[str]
) -> None:
    api.login()
    assert api.post(EMPTY, json={}).status_code == 422  # no word: the body is refused
    ok = api.post(EMPTY, json={"typed": PHRASE})
    assert ok.status_code == 200 and ok.json()["removed"] == 2
    assert effects == [f"empty:{PHRASE}:session"]
    lines = db_rows(
        api.settings, "SELECT session_id, auth_time FROM audit WHERE action = 'trash.empty'"
    )
    assert len(lines) == 2  # the guard writes one per authorised call, the empty body included
    assert all(session_id and auth_time for session_id, auth_time in lines)
    api.clock.advance(timedelta(minutes=20))  # stale: the fresh session is the right
    stale = api.post(EMPTY, json={"typed": PHRASE})
    assert stale.status_code == 401
    assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert effects == [f"empty:{PHRASE}:session"]


def test_empty_trash_and_lift_refuse_keys_members_and_children(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    effects: list[str],
) -> None:
    key = make_key(monkeypatch, capsys, api)
    for path in (EMPTY, LIFT):
        refused = api.client.post(
            path, json={"typed": "x"}, headers={"Authorization": f"Bearer {key}"}
        )
        assert refused.status_code == 403
        assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
    for role in ("member", "child"):
        assert api.login(add_user(api, role)).status_code == 200
        for path in (EMPTY, LIFT, SPACE):
            body = {"estimated_bytes": 1} if path == SPACE else {"typed": "x"}
            assert api.post(path, json=body).status_code == 403, (role, path)
    assert effects == []  # nothing reached a service


def test_the_new_routes_are_found_behind_their_reserved_action(api: AuthEnv) -> None:
    routes = {(m, p): a for m, p, a in real_reserved_routes(api)}
    assert routes[("POST", "/api/v1/trash/empty")] == "trash.empty"
    assert routes[("POST", "/api/v1/tombstones/x/lift")] == "tombstone.lift"
    assert routes[("POST", "/api/v1/units/x/purge")] == "purge.now"


def test_purge_now_ui_requires_typed_confirmation(api: AuthEnv, effects: list[str]) -> None:
    """« Effacer maintenant » by HTTP: a reserved action, a typed word in the body (without it,
    nothing reaches the service), the same service method as `purge --now`."""
    api.login()
    assert api.post(PURGE, json={}).status_code == 422
    assert api.post(PURGE, json={"typed": ""}).status_code == 422
    assert effects == []
    assert api.post(PURGE, json={"typed": "u1"}).status_code == 200
    assert effects == ["purge:u1:session"]
    assert OPS_METHODS["ops.purge"][1] == "purge.now"  # the CLI door: the same method, same action


def test_lift_over_http_names_the_sequence_and_the_word(api: AuthEnv, effects: list[str]) -> None:
    api.login()
    assert api.post(LIFT, json={}).status_code == 422
    assert api.post("/api/v1/tombstones/0/lift", json={"typed": "x"}).status_code == 422
    assert api.post("/api/v1/tombstones/abc/lift", json={"typed": "x"}).status_code == 422
    assert api.post(LIFT, json={"typed": "lift 7"}).status_code == 200
    assert effects == ["lift:7:lift 7:session"]


def test_add_refused_when_estimated_size_exceeds_free_space(api: AuthEnv) -> None:
    """60 GiB estimated, 40 GiB free: refused with the size, the free space and the action; no
    write: the database is exactly what it was."""
    api.login()
    before = db_dump(api.settings)

    refused = api.post(SPACE, json={"estimated_bytes": 60 * GIB})

    assert refused.status_code == 409
    body = refused.json()
    assert body["type"] == "/api/v1/problems/library.space-insufficient"
    assert (body["estimated_bytes"], body["free_bytes"], body["reserve_bytes"]) == (
        60 * GIB, 40 * GIB, 10 * GIB,
    )  # fmt: skip
    assert body["reclaim_action"] == "empty_trash"
    assert "corbeille" in body["action"].lower()  # the text names "Vider la corbeille"
    assert db_dump(api.settings) == before
    fits = api.post(SPACE, json={"estimated_bytes": 30 * GIB})
    assert fits.status_code == 200 and fits.json()["fits"] is True
    assert api.post(SPACE, json={"estimated_bytes": -1}).status_code == 422
    assert api.post(SPACE, json={"estimated_bytes": "lots"}).status_code == 422


# -- the CLI door ------------------------------------------------------------------------------


def _subparsers(parser: Any) -> dict[str, Any]:
    action = next(a for a in parser._actions if a.__class__.__name__ == "_SubParsersAction")
    return dict(action.choices)


def test_the_cli_has_one_door_per_action_and_it_is_the_reserved_method() -> None:
    commands = _subparsers(build_parser())
    assert {"trash", "tombstone"} <= set(commands)
    assert "empty" in _subparsers(commands["trash"])
    assert "lift" in _subparsers(commands["tombstone"])
    assert OPS_METHODS["ops.trash_empty"][1] == "trash.empty"
    assert OPS_METHODS["ops.tombstone_lift"][1] == "tombstone.lift"


def test_trash_empty_and_lift_refuse_without_their_word(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    assert run_cli(monkeypatch, settings, "trash", "empty") == 2
    assert "--confirm" in capsys.readouterr().err
    assert run_cli(monkeypatch, settings, "tombstone", "lift", "--seq", "3") == 2
    assert "--confirm" in capsys.readouterr().err


def test_every_new_action_is_declared_and_enumerated() -> None:
    """EF-USR-024: « Vider la corbeille » and the lift are in the verified enumeration."""
    for name in ("trash.empty", "tombstone.lift", "purge.now"):
        assert name in RESERVED_ACTIONS
