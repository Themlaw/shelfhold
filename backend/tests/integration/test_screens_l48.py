"""M3 L48 (EF-USR-006, EF-UX-004, EF-BIB-021, EF-BIB-052): the doors behind the three screens
"À classer" (one Edition alone), "Connect a device" and "Corbeille".

The services behind the routes are recorded: what is tried here is who may reach them, with which
credential and how fresh, and that nothing secret or hidden comes back. Every "never" is a call
that tries to break it: no session, a key, a member, a child, a member that holds no right to
classify, a stale session, a missing CSRF token, another Edition's id.
"""

import json
import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import Principal
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env
from tests.integration.test_reserved_real_path import add_user, make_key

EDITION = "01JEDIT0000000000000000001"
RATE = f"/api/v1/editions/{EDITION}/rating"
TRASH = "/api/v1/trash"
CONNECT = "/api/v1/devices/connect"
ENTRY_POINTS = {
    "targets": [
        {
            "target_id": "01JTARG0000000000000000001",
            "name": "source-a",
            "kover": "https://kavita.example",
            "opds": "https://kavita.example/api/opds",
            "kosync": "https://kavita.example/api/koreader",
        }
    ]
}
TRASH_BODY = {
    "entries": [
        {
            "deletion_id": "01JDELE0000000000000000001", "unit_id": "01JUNIT0000000000000000001",
            "level": "delete_content", "files": 2, "bytes": 1234,
            "deleted_at": "2026-11-10T10:00:00", "purge_after": "2026-12-10T10:00:00",
            "irreplaceable": True,
        },
    ],
    "total_bytes": 1234,
}  # fmt: skip


class _Rating:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, str | None, str]] = []

    def rate(
        self, age: str, *, work_id: str | None, edition_id: str | None, principal: Principal
    ) -> dict[str, Any]:
        self.calls.append((age, work_id, edition_id, principal.credential.value))
        return {"target": "edition:1", "label": "age.min.13", "effective": 13,
                "effective_x": False, "withdrawals": []}  # fmt: skip


class _Library:
    def trash_list(self) -> dict[str, Any]:
        return TRASH_BODY

    def device_entry_points(self) -> dict[str, Any]:
        return ENTRY_POINTS


class _Ops:
    def __init__(self) -> None:
        self.rating = _Rating()
        self.library = _Library()

    def paused_queues(self) -> list[str]:
        return []


@pytest.fixture
def ops() -> _Ops:
    return _Ops()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        yield env


@pytest.mark.req("EF-USR-006", "EF-USR-022")
def test_rate_one_edition_reserved_fresh_and_csrf(api: AuthEnv, ops: _Ops) -> None:
    body = {"age": "13+"}
    assert api.client.post(RATE, json=body).status_code in (401, 403)  # no session
    api.login()
    assert api.client.post(RATE, json=body).status_code == 403  # session, no CSRF token
    wrong = api.client.post(RATE, json=body, headers={CSRF_HEADER: "not-the-token"})
    assert wrong.status_code == 403
    assert ops.rating.calls == []
    api.clock.advance(timedelta(minutes=20))  # stale: the fresh session is the right
    stale = api.post(RATE, json=body)
    assert stale.status_code == 401 and stale.json()["type"].endswith("/reauth-required")
    assert ops.rating.calls == []
    assert api.login().status_code == 200
    done = api.post(RATE, json=body)
    assert done.status_code == 200
    assert ops.rating.calls == [("13+", None, EDITION, "session")]


@pytest.mark.req("EF-USR-006", "EF-BIB-036")
def test_rate_one_edition_refused_to_keys_members_and_children(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    ops: _Ops,
) -> None:
    key = make_key(monkeypatch, capsys, api)
    refused = api.client.post(RATE, json={"age": "0+"}, headers={"Authorization": f"Bearer {key}"})
    assert refused.status_code == 403
    assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
    for role in ("member", "child"):  # no right to classify: approval without the right
        assert api.login(add_user(api, role)).status_code == 200
        assert api.post(RATE, json={"age": "0+"}).status_code == 403, role
    assert ops.rating.calls == []


@pytest.mark.req("EF-USR-006")
def test_rate_one_edition_refuses_a_bad_age_and_a_malformed_id(api: AuthEnv, ops: _Ops) -> None:
    api.login()
    assert api.post(RATE, json={}).status_code == 422
    assert api.post(RATE, json={"age": ""}).status_code == 422
    assert api.post(f"/api/v1/editions/{'x' * 80}/rating", json={"age": "0+"}).status_code == 422
    assert ops.rating.calls == []


@pytest.mark.req("EF-UX-004", "ENF-SEC-010")
def test_connect_device_entry_points_are_the_admins_and_hold_no_secret(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
) -> None:
    assert api.client.get(CONNECT).status_code == 401
    for role in ("member", "child"):
        assert api.login(add_user(api, role)).status_code == 200
        assert api.client.get(CONNECT).status_code == 403, role
    assert api.login().status_code == 200
    answer = api.client.get(CONNECT)
    assert answer.status_code == 200
    text = json.dumps(answer.json()).lower()
    for word in ("apikey", "api_key", "token", "secret", "password", "shk_", "@"):
        assert word not in text, word
    [target] = answer.json()["targets"]
    assert set(target) == {"target_id", "name", "kover", "opds", "kosync"}
    assert not re.search(r"[?#]|/api/opds/.|/api/koreader/.", text)  # no key in the path either
    # a read by a key is no reserved action, but it never comes back with a key in it either
    key = make_key(monkeypatch, capsys, api)
    keyed = api.client.get(CONNECT, headers={"Authorization": f"Bearer {key}"})
    assert key not in keyed.text


@pytest.mark.req("EF-BIB-021")
def test_trash_list_is_the_admins_and_names_no_path(api: AuthEnv) -> None:
    assert api.client.get(TRASH).status_code == 401
    for role in ("member", "child"):
        assert api.login(add_user(api, role)).status_code == 200
        assert api.client.get(TRASH).status_code == 403, role
    assert api.login().status_code == 200
    answer = api.client.get(TRASH)
    assert answer.status_code == 200
    [entry] = answer.json()["entries"]
    assert entry["irreplaceable"] is True and entry["purge_after"]
    assert "path" not in entry and "snapshot" not in entry
