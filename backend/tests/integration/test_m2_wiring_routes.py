"""M2 wiring: the admin routes of the commands above, over the real application with a service
the test controls. A key reaches the unreserved ones with the `admin` scope and none of the
reserved ones; the service is never called for a refusal."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from tests.auth_env import AuthEnv, auth_env, run_cli

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
EDITION = "01JEDITION000000000000000000"
UNIT = "01JUNIT00000000000000000000"
CODE = "a" * 16 + "." + "b" * 64 + "." + "c" * 32


class _Editions:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def lock(self, edition: str, field: str, value: str | None, **kw: Any) -> dict[str, Any]:
        self.calls.append(("lock", edition, field, value, kw))
        return {"edition_id": edition, "field": field, "locked": True, "kept": value is None}

    def unlock(self, edition: str, field: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("unlock", edition, field, kw))
        return {"edition_id": edition, "field": field, "locked": False, "kept": True}

    def renumber(self, edition: str, provider: str, old: str, new: str, **kw: Any) -> dict[str, Any]:
        self.calls.append(("renumber", edition, provider, old, new, kw))
        return {"edition_id": edition, "provider": provider, "old": old, "new": new, "added": True}

    def listing(self, *, sort: str, limit: int, offset: int) -> list[dict[str, Any]]:
        self.calls.append(("list", sort, limit, offset))
        return [{"edition_id": EDITION, "work_id": "W", "lang": "en", "title": "A", "sort_key": "a"}]


class _Resplit:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def propose(self, parent: str, children: list[str], *, principal: Any) -> dict[str, Any]:
        self.calls.append(("propose", parent, children, principal.credential.value))
        return {"edition_id": EDITION, "parent": parent, "children": children, "code": CODE}

    def apply(self, parent: str, children: list[str], *, confirm: str, principal: Any) -> dict[str, Any]:
        self.calls.append(("apply", parent, children, confirm, principal.credential.value))
        return {"edition_id": EDITION, "parent": parent, "children": children, "added": 2}


class _Items:
    def show(self, item_id: str) -> dict[str, Any]:
        return {"id": item_id, "edition_id": EDITION, "kind": "volume", "label": "Vol.1",
                "closed": True, "units": 2, "kept": None, "kept_problem": "ranged.missing"}  # fmt: skip


class _Ops:
    def __init__(self) -> None:
        self.editions, self.resplit, self.export_items = _Editions(), _Resplit(), _Items()


@pytest.fixture
def ops() -> _Ops:
    return _Ops()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        yield env


def _key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
         *extra: str) -> dict[str, str]:  # fmt: skip
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "ci", *extra) == 0
    match = KEY.search(capsys.readouterr().out)
    assert match
    return {"Authorization": f"Bearer {match.group(0)}"}


@pytest.mark.req("EF-BIB-008", "EF-BIB-003", "ENF-I18N-003")
def test_field_locks_renumbering_and_the_sorted_list_are_admin_routes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv, ops: _Ops
) -> None:
    read = _key(monkeypatch, capsys, api.settings, "--read-only")
    paths = [
        ("post", f"/api/v1/editions/{EDITION}/fields/title/lock", {"value": "Mon titre"}),
        ("post", f"/api/v1/editions/{EDITION}/fields/title/unlock", {}),
        ("post", f"/api/v1/editions/{EDITION}/external-ids/renumber",
         {"provider": "anilist", "old": "1", "new": "ab"}),
    ]  # fmt: skip
    for _verb, path, body in paths:  # a read-only key reaches none of them
        refused = api.client.post(path, json=body, headers=read)
        assert refused.status_code == 403
    assert api.client.get("/api/v1/editions", headers=read).status_code == 403
    assert ops.editions.calls == []
    api.login()
    lock = api.post(paths[0][1], json=paths[0][2])
    assert lock.status_code == 200 and lock.json()["locked"] is True
    assert api.post(paths[1][1], json=paths[1][2]).json()["locked"] is False
    assert api.post(paths[2][1], json=paths[2][2]).json()["added"] is True
    listed = api.client.get("/api/v1/editions?sort=title&limit=5&offset=1")
    assert listed.status_code == 200 and listed.json()["items"][0]["title"] == "A"
    assert ("list", "title", 5, 1) in ops.editions.calls
    for bad in ("sort=price", "limit=0", "limit=100000", "offset=-1"):
        assert api.client.get(f"/api/v1/editions?{bad}").status_code == 422
    too_long = api.post(paths[0][1], json={"value": "x" * 5000})
    assert too_long.status_code == 422


@pytest.mark.req("EF-BIB-044", "EF-USR-024")
def test_resplit_routes_are_reserved_never_for_a_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv, ops: _Ops
) -> None:
    admin = _key(monkeypatch, capsys, api.settings)
    body = {"parent_unit_id": UNIT, "child_unit_ids": [UNIT[:-1] + "1"]}
    for path, extra in (("/api/v1/coverage/resplit/propose", {}),
                        ("/api/v1/coverage/resplit/apply", {"confirm": CODE})):  # fmt: skip
        refused = api.client.post(path, json={**body, **extra}, headers=admin)
        assert refused.status_code == 403
        assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
    assert ops.resplit.calls == []
    api.login()
    proposed = api.post("/api/v1/coverage/resplit/propose", json=body)
    assert proposed.status_code == 200 and proposed.json()["code"] == CODE
    applied = api.post("/api/v1/coverage/resplit/apply", json={**body, "confirm": CODE})
    assert applied.status_code == 200 and applied.json()["added"] == 2
    assert [c[0] for c in ops.resplit.calls] == ["propose", "apply"]
    # no confirmation, no children, too many children: refused before the service
    assert api.post("/api/v1/coverage/resplit/apply", json=body).status_code == 422
    assert api.post("/api/v1/coverage/resplit/propose",
                    json={"parent_unit_id": UNIT, "child_unit_ids": []}).status_code == 422  # fmt: skip
    many = {"parent_unit_id": UNIT, "child_unit_ids": ["x"] * 500}
    assert api.post("/api/v1/coverage/resplit/propose", json=many).status_code == 422
    assert len(ops.resplit.calls) == 2


@pytest.mark.req("EF-BIB-044")
def test_export_item_route_is_for_an_admin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    read = _key(monkeypatch, capsys, api.settings, "--read-only")
    assert api.client.get(f"/api/v1/export-items/{UNIT}", headers=read).status_code == 403
    api.login()
    shown = api.client.get(f"/api/v1/export-items/{UNIT}")
    assert shown.status_code == 200 and shown.json()["kept_problem"] == "ranged.missing"
