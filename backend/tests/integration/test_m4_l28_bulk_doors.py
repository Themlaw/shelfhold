"""M4 L28 (EF-BIB-023, EF-BIB-036, EF-USR-024): the two doors of a bulk rating.

`POST /api/v1/bulk/rate/preview` freezes a selection into an order, `POST
/api/v1/bulk/rate/{order_id}/apply` queues the one job (202 and a `job_id`, like the other orders
of the queue). Both are the reserved action `rate`: a fresh admin session holding the right to
classify, never a key (read-only or not), never a member or a child, and the answer is never kept
for an idempotent replay.

The routes are the real ones; the service behind them is a recorder, so what is tried here is who
reaches it, and what it is given.
"""

import re
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.ops.errors import StalePreviewError, UnknownSubjectError
from tests.auth_env import AuthEnv, auth_env, db_rows, run_cli
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-BIB-023", "EF-BIB-036", "EF-USR-024")

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
WORK = "01JWORK0000000000000000001"
ORDER = "01JORDER000000000000000001"
JOB = "01JJOB0000000000000000000A"
PREVIEW = "/api/v1/bulk/rate/preview"
APPLY = f"/api/v1/bulk/rate/{ORDER}/apply"
TOKEN = "1-abcd1234"  # noqa: S105 (test fixture)
CLOSED = ("/api/v1/problems/credential-not-allowed", "/api/v1/problems/forbidden")


class _Bulk:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def rate_preview(
        self, *, age: str, principal: Any, work_ids: list[str] | None = None,
        filter: dict[str, Any] | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        self.calls.append(("preview", age, work_ids, filter, principal.credential.value))
        return {
            "order_id": ORDER, "age": age, "token": TOKEN, "skipped": 0,
            "expires_at": "2026-11-17T12:15:00",
            "items": [{"work_id": WORK, "title": "<b>Un titre</b>", "state": "rating_pending"}],
        }  # fmt: skip

    def rate_queue(self, *, order_id: str, confirm: str, principal: Any) -> str:
        self.calls.append(("apply", order_id, confirm, principal.credential.value))
        if order_id != ORDER:
            raise UnknownSubjectError("no such order")
        if confirm != TOKEN:
            raise StalePreviewError("changed")
        return JOB


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[tuple[AuthEnv, _Bulk]]:
    bulk = _Bulk()
    with auth_env(monkeypatch, settings, ops_services=SimpleNamespace(bulk=bulk)) as env:
        yield env, bulk


def _key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
    *extra: str,
) -> dict[str, str]:  # fmt: skip
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "k", *extra) == 0
    found = KEY.search(capsys.readouterr().out)
    assert found
    return {"Authorization": f"Bearer {found.group(0)}"}


DOORS = (
    (PREVIEW, {"age": "13+", "work_ids": [WORK]}),
    (APPLY, {"confirm": TOKEN}),
)


def test_preview_then_apply_returns_202_and_a_job_id(api: tuple[AuthEnv, _Bulk]) -> None:
    env, bulk = api
    env.login()
    seen = env.post(PREVIEW, json={"age": "13+", "work_ids": [WORK]})
    assert seen.status_code == 200, seen.text
    body = seen.json()
    assert body["order_id"] == ORDER and body["token"] == TOKEN
    assert body["items"][0]["work_id"] == WORK and body["skipped"] == 0
    done = env.post(APPLY, json={"confirm": TOKEN})
    assert done.status_code == 202, done.text
    assert done.json() == {"job_id": JOB, "status_url": f"/api/v1/jobs/{JOB}"}
    assert done.headers["location"] == f"/api/v1/jobs/{JOB}"
    assert [c[0] for c in bulk.calls] == ["preview", "apply"]
    assert bulk.calls[0][2:4] == ([WORK], None)  # the selection is what the client named


def test_a_filter_is_passed_as_a_filter_and_never_with_a_list(api: tuple[AuthEnv, _Bulk]) -> None:
    env, bulk = api
    env.login()
    done = env.post(
        PREVIEW, json={"age": "13+", "filter": {"state": "rating_pending", "text": "a"}}
    )
    assert done.status_code == 200
    assert bulk.calls[-1][2:4] == (None, {"state": "rating_pending", "text": "a"})


@pytest.mark.parametrize(
    "body",
    [
        {"age": "13+"},  # no selection
        {"age": "13+", "work_ids": []},  # an empty one
        {"age": "13+", "work_ids": [WORK], "filter": {"text": "a"}},  # two selections
        {"age": "13+", "work_ids": ["not-an-id"]},
        {"age": "13+", "work_ids": ["01JWORK000000000000000000" + "1" * 5]},
        {"age": "13+", "work_ids": [WORK] * 1001},  # beyond the bound
        {"age": "", "work_ids": [WORK]},
        {"age": "13+", "filter": {"state": "unknown"}},
        {"age": "13+", "filter": {"text": "x" * 201}},
        {"age": "13+", "work_ids": [WORK], "everything": True},  # nothing outside the contract
    ],
)
def test_a_selection_outside_the_contract_is_refused_before_the_service(
    api: tuple[AuthEnv, _Bulk], body: dict[str, Any]
) -> None:
    env, bulk = api
    env.login()
    assert env.post(PREVIEW, json=body).status_code == 422
    assert bulk.calls == []


def test_the_apply_names_only_the_order_and_its_confirmation(api: tuple[AuthEnv, _Bulk]) -> None:
    """No list of Works can be added to the apply: the order is the selection (IDOR, widening)."""
    env, bulk = api
    env.login()
    wide = env.post(APPLY, json={"confirm": TOKEN, "work_ids": [WORK], "age": "0+"})
    assert wide.status_code == 422 and bulk.calls == []
    assert env.post(APPLY, json={}).status_code == 422


def test_an_unknown_order_and_a_stale_token_are_refused_without_echo(
    api: tuple[AuthEnv, _Bulk],
) -> None:
    env, _bulk = api
    env.login()
    other = "01JOTHER00000000000000000A"
    missing = env.post(f"/api/v1/bulk/rate/{other}/apply", json={"confirm": TOKEN})
    assert missing.status_code == 404 and other not in missing.text  # the id asked is not echoed
    stale = env.post(APPLY, json={"confirm": "9-ffffffff"})
    assert stale.status_code == 409
    garbage = env.post("/api/v1/bulk/rate/..%2F..%2Fx/apply", json={"confirm": TOKEN})
    assert garbage.status_code in (404, 422)


def test_attacks_a_key_a_read_only_key_a_member_a_child_nobody_reach_no_service(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    api: tuple[AuthEnv, _Bulk],
) -> None:  # fmt: skip
    env, bulk = api
    keys = [
        _key(monkeypatch, capsys, env.settings),
        _key(monkeypatch, capsys, env.settings, "--read-only"),
    ]
    for path, body in DOORS:
        assert env.client.post(path, json=body).status_code in (401, 403), path  # nobody
        for headers in keys:  # an admin key, a read-only key: whatever its scope
            keyed = env.client.post(path, json=body, headers=headers)
            assert keyed.status_code == 403 and keyed.json()["type"] in CLOSED, path
    env.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(env, f"{role}1", role, ip, **extra)
        for path, body in DOORS:
            refused = who.post(path, json=body, headers={"X-CSRF-Token": env.csrf(who)})
            assert refused.status_code == 403, (role, path)
            assert JOB not in refused.text and ORDER not in refused.text
    assert bulk.calls == []  # not one refusal reached the service


def test_no_csrf_token_no_order(api: tuple[AuthEnv, _Bulk]) -> None:
    env, bulk = api
    env.login()
    for path, body in DOORS:
        assert env.client.post(path, json=body).status_code == 403
        wrong = env.client.post(path, json=body, headers={"X-CSRF-Token": "not-the-token"})
        assert wrong.status_code == 403
    assert bulk.calls == []


@pytest.mark.parametrize("hardened", [False, True])
def test_bulk_rate_requires_a_session_fresher_than_900_s_in_every_profile(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, hardened: bool
) -> None:
    bulk = _Bulk()
    with auth_env(
        monkeypatch, settings, ops_services=SimpleNamespace(bulk=bulk), hardened=hardened
    ) as env:
        env.login(remember=False)
        assert env.post(PREVIEW, json=DOORS[0][1]).status_code == 200  # just opened: fresh
        env.clock.advance(timedelta(minutes=20))
        for path, body in DOORS:
            stale = env.post(path, json=body)
            assert stale.status_code == 401, path
            assert stale.json()["type"] == "/api/v1/problems/reauth-required"
        assert len(bulk.calls) == 1  # the stale ones never reached the service


def test_a_reserved_route_never_stores_a_replay(api: tuple[AuthEnv, _Bulk]) -> None:
    """The same `Idempotency-Key` twice is two passes through the guards, never a stored answer:
    an apply that was replayed from the table would hand out a job id to a session that no longer
    holds the right."""
    env, bulk = api
    env.login()
    headers = {"Idempotency-Key": "-".join(["same", "key", "0001", "abcdef"])}
    first = env.post(APPLY, json={"confirm": TOKEN}, headers=headers)
    second = env.post(APPLY, json={"confirm": TOKEN}, headers=headers)
    assert first.status_code == second.status_code == 202
    assert "idempotent-replayed" not in second.headers
    assert len([c for c in bulk.calls if c[0] == "apply"]) == 2
    assert db_rows(env.settings, "SELECT COUNT(*) FROM idempotency_key") == [(0,)]
