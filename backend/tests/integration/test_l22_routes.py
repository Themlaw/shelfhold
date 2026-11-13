"""M3 L22: the HTTP doors of the merge of Works, "Séparer", the plan that moves an exported series,
the merge plan of Editions and what a merge of contributors proposes. Every one is a reserved action
(a fresh admin session, never a key, never a member or a child) and the service is never called for
a refusal. The attacks: a key, a member, a child, no session, no confirmation, a wrong verb."""

from __future__ import annotations

import re
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, run_cli
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-BIB-019", "EF-EXP-062", "EF-BIB-049", "EF-USR-024")

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
A, B = "01JWORKA000000000000000000", "01JWORKB000000000000000000"
REFUSED = ("/api/v1/problems/credential-not-allowed", "/api/v1/problems/forbidden")
MERGE = "01JMERGE00000000000000000A"
FP = "f" * 16


class _WorkMerge:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def merge(self, keep: str, drop: str, *, confirm: str | None, principal: Any) -> dict[str, Any]:
        self.calls.append(("merge", keep, drop, confirm, principal.credential.value))
        return {"keep_id": keep, "drop_id": drop, "editions": [], "blockers": [],
                "keep_age": None, "drop_age": None, "merged_age": None, "unrated_after": False,
                "mergeable": True, "fingerprint": FP, "applied": confirm is not None,
                "merge_id": MERGE if confirm else None}  # fmt: skip

    def separate(self, merge_id: str, *, principal: Any) -> dict[str, Any]:
        self.calls.append(("separate", merge_id, principal.credential.value))
        return {
            "merge_id": merge_id,
            "survivor_id": A,
            "restored_id": B,
            "shelves": {},
            "waiting": {},
            "survivor_age": "6+",
            "restored_age": "6+",
            "series_plan": {
                "work_id": B,
                "moves": [],
                "blockers": [],
                "path_changes": 0,
                "emptied": [],
                "mergeable": True,
                "fingerprint": FP,
                "applied": False,
            },
        }

    def series_reassign(
        self, work_id: str, *, confirm: str | None, principal: Any
    ) -> dict[str, Any]:
        self.calls.append(("series", work_id, confirm, principal.credential.value))
        return {"work_id": work_id, "moves": [], "blockers": [], "path_changes": 0, "emptied": [],
                "mergeable": True, "fingerprint": FP, "applied": confirm is not None}  # fmt: skip

    def attach_series(
        self, work_id: str, series_id: str, position: str, *, principal: Any
    ) -> dict[str, Any]:
        self.calls.append(("attach", work_id, series_id, position))
        return {"work_id": work_id, "series_id": series_id, "position": position,
                "reassignment": {"work_id": work_id, "moves": [], "blockers": [],
                                 "path_changes": 0, "emptied": [], "mergeable": True,
                                 "fingerprint": FP, "applied": False}}  # fmt: skip


class _EditionMerge:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def merge(self, keep: str, drop: str, *, confirm: str | None, principal: Any) -> dict[str, Any]:
        self.calls.append(("edition", keep, drop, confirm))
        return {"keep_id": keep, "drop_id": drop, "work_id": A, "units": [], "volumes": [],
                "bindings": [], "blockers": [], "tombstoned_units": 0, "tombstoned_ids": 0,
                "keep_age": None, "drop_age": None, "merged_age": None, "unrated_after": False,
                "mergeable": True, "fingerprint": FP, "applied": confirm is not None}  # fmt: skip

    def proposals(self, keep: str, drop: str, *, principal: Any) -> list[dict[str, Any]]:
        self.calls.append(("proposals", keep, drop))
        return [self.merge(A, B, confirm=None, principal=principal)]


class _Ops:
    def __init__(self) -> None:
        self.work_merge, self.edition_merge = _WorkMerge(), _EditionMerge()

    @property
    def calls(self) -> list[tuple[Any, ...]]:
        return [*self.work_merge.calls, *self.edition_merge.calls]


@pytest.fixture
def ops() -> _Ops:
    return _Ops()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        yield env


DOORS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("/api/v1/works/merge", {"keep_id": A, "drop_id": B}),
    ("/api/v1/works/merge", {"keep_id": A, "drop_id": B, "confirm": FP}),
    (f"/api/v1/works/merges/{MERGE}/separate", {}),
    ("/api/v1/series-reassignments", {"work_id": A}),
    ("/api/v1/series-reassignments", {"work_id": A, "confirm": FP}),
    ("/api/v1/editions/merge", {"keep_id": A, "drop_id": B}),
    ("/api/v1/editions/merge", {"keep_id": A, "drop_id": B, "confirm": FP}),
    ("/api/v1/contributors/merge-proposals", {"keep_id": A, "drop_id": B}),
)


def _key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
         *extra: str) -> dict[str, str]:  # fmt: skip
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "ci", *extra) == 0
    match = KEY.search(capsys.readouterr().out)
    assert match
    return {"Authorization": f"Bearer {match.group(0)}"}


def test_no_key_no_member_no_child_no_anonymous_reaches_a_door(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv, ops: _Ops
) -> None:
    admin_key = _key(monkeypatch, capsys, api.settings)
    scoped = _key(monkeypatch, capsys, api.settings, "--read-only")
    for path, body in DOORS:
        assert api.client.post(path, json=body).status_code in (401, 403)  # no session
        for headers in (admin_key, scoped):  # not even a key with the admin scope
            refused = api.client.post(path, json=body, headers=headers)
            assert refused.status_code == 403, path
            assert refused.json()["type"] in REFUSED, (path, refused.json())
    api.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(api, f"{role}1", role, ip, **extra)
        for path, body in DOORS:
            refused = who.post(path, json=body, headers={CSRF_HEADER: api.csrf(who)})
            assert refused.status_code == 403, (role, path)
            assert refused.json()["type"] == "/api/v1/problems/forbidden", (role, path)
    assert ops.calls == []  # the service was never reached by a refusal


def test_admin_session_plans_then_applies_and_separates(api: AuthEnv, ops: _Ops) -> None:
    api.login()
    plan = api.post("/api/v1/works/merge", json={"keep_id": A, "drop_id": B})
    assert plan.status_code == 200 and plan.json()["applied"] is False
    done = api.post("/api/v1/works/merge", json={"keep_id": A, "drop_id": B, "confirm": FP})
    assert done.status_code == 200 and done.json()["merge_id"] == MERGE
    split = api.post(f"/api/v1/works/merges/{MERGE}/separate", json={})
    assert split.status_code == 200 and split.json()["restored_id"] == B
    series = api.post("/api/v1/series-reassignments", json={"work_id": A, "confirm": FP})
    assert series.status_code == 200 and series.json()["applied"] is True
    shown = api.post("/api/v1/editions/merge", json={"keep_id": A, "drop_id": B})
    assert shown.status_code == 200 and shown.json()["applied"] is False
    proposed = api.post("/api/v1/contributors/merge-proposals", json={"keep_id": A, "drop_id": B})
    assert proposed.status_code == 200 and proposed.json()["items"][0]["applied"] is False
    # nothing was applied by a proposal: the Edition plans are shown, never run without a code
    assert all(c[3] is None for c in ops.edition_merge.calls if c[0] == "edition")


def test_a_door_refuses_what_it_cannot_read_before_the_service(api: AuthEnv, ops: _Ops) -> None:
    api.login()
    for path, body in (
        ("/api/v1/works/merge", {"keep_id": A}),  # nothing to merge into it
        ("/api/v1/works/merge", {"keep_id": A, "drop_id": B, "confirm": "x" * 500}),
        ("/api/v1/works/merge", {"keep_id": A, "drop_id": B, "also": 1}),  # unknown field
        ("/api/v1/series-reassignments", {}),
        ("/api/v1/contributors/merge-proposals", {"keep_id": "", "drop_id": B}),
    ):
        assert api.post(path, json=body).status_code == 422, path
    assert ops.calls == []
    # a verb that is not the door's
    assert api.client.get("/api/v1/works/merge").status_code in (405, 401, 403)
    assert api.client.get(f"/api/v1/works/merges/{MERGE}/separate").status_code in (405, 401, 403)


def test_late_attach_door_is_for_an_admin_and_validates_its_position(
    api: AuthEnv, ops: _Ops
) -> None:
    path, body = f"/api/v1/works/{A}/series", {"series_id": B, "position": "1.5"}
    assert api.client.post(path, json=body).status_code in (401, 403)
    api.login()
    for role, ip in (("member", "10.0.0.7"), ("child", "10.0.0.8")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(api, f"late{role}", role, ip, **extra)
        refused = who.post(path, json=body, headers={CSRF_HEADER: api.csrf(who)})
        assert refused.status_code == 403, role
    assert ops.work_merge.calls == []
    for bad in ({"series_id": B, "position": "1.2.3"}, {"series_id": B, "position": "x"},
                {"series_id": B}, {"series_id": B, "position": "1", "extra": 1}):  # fmt: skip
        assert api.post(path, json=bad).status_code == 422
    done = api.post(path, json=body)
    assert done.status_code == 200 and done.json()["reassignment"]["applied"] is False
    assert ops.work_merge.calls == [("attach", A, B, "1.5")]
