"""EF-BIB-021, EF-USR-024 (L25): the three routes of the deletion are reserved. A key, a member
and a child are refused before the service is reached; a fresh admin session reaches it, a stale
one is asked to authenticate again; each answer is a problem of the catalog."""

from collections.abc import Iterator
from datetime import timedelta

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import Principal
from tests.auth_env import AuthEnv, auth_env
from tests.integration.test_reserved_real_path import add_user, make_key

PREVIEW = "/api/v1/deletions/preview"
APPLY = "/api/v1/deletions"
RESTORE = "/api/v1/units/u1/restore"
BODY = {"level": "delete_content", "unit_ids": ["u1"]}
APPLY_BODY = {**BODY, "version": "v1", "typed": {"u1": "u1"}}


class _Library:
    def __init__(self, effects: list[str]) -> None:
        self._effects = effects

    def delete_preview(self, *, level: str, unit_ids: list[str]) -> dict[str, object]:
        self._effects.append(f"preview-{level}-{len(unit_ids)}")
        return {"version": "v1", "units": []}

    def delete_apply(self, *, principal: Principal, typed: dict[str, str], **kw: object) -> dict:
        self._effects.append(
            f"apply-by-{principal.credential}-typed-{sorted(typed)}-{kw['if_match']}"
        )
        return {"moved": 0}

    def delete_restore(
        self, *, unit_id: str, principal: Principal, deletion_id: str | None, partial: bool
    ) -> dict[str, object]:
        self._effects.append(f"restore-{unit_id}")
        return {"restored": 0}


class _Services:
    def __init__(self, effects: list[str]) -> None:
        self.library = _Library(effects)


@pytest.fixture
def effects() -> list[str]:
    return []


@pytest.fixture
def api(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, effects: list[str]
) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=_Services(effects)) as env:
        yield env


@pytest.mark.req("EF-BIB-021", "EF-USR-024")
def test_deletion_routes_refuse_keys_members_and_children(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    effects: list[str],
) -> None:
    key = make_key(monkeypatch, capsys, api)
    for path, body in ((PREVIEW, BODY), (APPLY, APPLY_BODY), (RESTORE, {})):
        keyed = api.client.post(path, json=body, headers={"Authorization": f"Bearer {key}"})
        assert keyed.status_code == 403
        assert keyed.json()["type"] == "/api/v1/problems/credential-not-allowed"
        assert api.client.post(path, json=body).status_code == 401  # nobody at all
    for role in ("member", "child"):
        assert api.login(add_user(api, role)).status_code == 200
        for path, body in ((PREVIEW, BODY), (APPLY, APPLY_BODY), (RESTORE, {})):
            refused = api.post(path, json=body)
            assert refused.status_code == 403
            assert refused.json()["type"] == "/api/v1/problems/forbidden"
    assert effects == []


@pytest.mark.req("EF-BIB-021", "EF-USR-024")
def test_deletion_routes_need_a_fresh_admin_session(api: AuthEnv, effects: list[str]) -> None:
    api.login()
    assert api.post(PREVIEW, json=BODY).status_code == 200
    applied = api.post(APPLY, json=APPLY_BODY)
    assert applied.status_code == 200
    assert api.post(RESTORE, json={}).status_code == 200
    assert effects == [
        "preview-delete_content-1",
        "apply-by-session-typed-['u1']-v1",
        "restore-u1",
    ]
    api.clock.advance(timedelta(minutes=20))
    for path, body in ((PREVIEW, BODY), (APPLY, APPLY_BODY), (RESTORE, {})):
        stale = api.post(path, json=body)
        assert stale.status_code == 401
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert len(effects) == 3  # the stale session reached nothing
    api.login()
    for bad in ({**APPLY_BODY, "level": "everything"}, {**APPLY_BODY, "unit_ids": []},
                {"level": "unfollow", "unit_ids": ["u1"]}):  # fmt: skip
        assert api.post(APPLY, json=bad).status_code == 422  # no version: not applied
    assert len(effects) == 3
