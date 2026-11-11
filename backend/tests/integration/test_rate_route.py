"""L11 (EF-BIB-036, EF-USR-024, EF-INT-014): `POST /api/v1/works/{id}/rating`.

Fixing an age is a reserved action: a fresh admin session reaches the service; an API key never
does, whatever its scope (a key that reads or requests, or even an administrator key), and the
service is not called. The route and the CLI call the same `RatingOps`.
"""

import re
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from tests.auth_env import PASSWORD, AuthEnv, auth_env, run_cli

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
PATH = "/api/v1/works/01JWORK0000000000000000000/rating"


class _Rating:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str]] = []

    def rate(
        self, age: str, *, work_id: str | None, edition_id: str | None, principal: Any
    ) -> dict:  # type: ignore[type-arg]
        self.calls.append((age, work_id, principal.credential.value))
        return {
            "target": f"work:{work_id}", "label": "age.min.13", "effective": 13,
            "effective_x": False,
            "withdrawals": [{"edition_id": "E1", "shelf_id": "S1", "reason": "above_ceiling"}],
        }  # fmt: skip


class _Ops:
    def __init__(self) -> None:
        self.rating = _Rating()


@pytest.fixture
def ops() -> _Ops:
    return _Ops()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        yield env


def _key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    *extra: str,
) -> dict[str, str]:
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "ci", *extra) == 0
    match = KEY.search(capsys.readouterr().out)
    assert match
    return {"Authorization": f"Bearer {match.group(0)}"}


@pytest.mark.req("EF-BIB-036", "EF-USR-024")
def test_read_or_request_key_setting_age_gets_403(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    ops: _Ops,
) -> None:
    read = _key(monkeypatch, capsys, api.settings, "--read-only")
    admin = _key(monkeypatch, capsys, api.settings)
    for bearer, problem in ((read, "forbidden"), (admin, "credential-not-allowed")):
        refused = api.client.post(PATH, json={"age": "13+"}, headers=bearer)
        assert refused.status_code == 403
        assert refused.json()["type"] == f"/api/v1/problems/{problem}"
    assert ops.rating.calls == []  # the service was never reached
    api.login()
    done = api.post(PATH, json={"age": "13+"})
    assert done.status_code == 200
    body = done.json()
    assert body["effective"] == 13 and body["withdrawals"][0]["reason"] == "above_ceiling"
    assert ops.rating.calls == [("13+", "01JWORK0000000000000000000", "session")]


@pytest.mark.req("EF-USR-022", "EF-BIB-036")
@pytest.mark.parametrize("hardened", [False, True])
def test_rating_needs_a_session_fresher_than_900_s_in_every_profile(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops, hardened: bool
) -> None:
    """Porteur, 2026-11-11: a stolen session must not file adult content as "0"."""
    from datetime import timedelta

    with auth_env(monkeypatch, settings, ops_services=ops, hardened=hardened) as env:
        env.login(remember=False)
        assert env.post(PATH, json={"age": "13+"}).status_code == 200  # just opened: fresh
        env.clock.advance(timedelta(minutes=20))
        stale = env.post(PATH, json={"age": "0+"})
        assert stale.status_code == 401
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
        assert len(ops.rating.calls) == 1  # the service was never reached
        reauth = env.post("/api/v1/auth/reauth", json={"method": "password", "password": PASSWORD})
        assert reauth.status_code == 200
        assert env.post(PATH, json={"age": "0+"}).status_code == 200
        assert len(ops.rating.calls) == 2
