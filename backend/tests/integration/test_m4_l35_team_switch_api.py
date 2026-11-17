"""The doors of the team switch: reserved, a fresh admin session, never a key (M4 L35, ENF-SEC-010).

`POST /api/v1/editions/{id}/team-switch` prepares the preview, `POST …/{switch}/apply` queues the
application (202 and the `job_id`, a write job), `GET …/{switch}` reads the report. All three are
the one reserved action `team.switch`. The doors are the real routes; the service behind them is a
recorder. The attacks: a key, a read-only key, a member, a child, nobody, a stale session, and the
identifier of somebody else's resource.
"""

import re
from collections.abc import Iterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.acquisition.application.team_switch import TeamSwitchRefusedError
from shelfhold.config import Settings
from tests.auth_env import AuthEnv, auth_env, run_cli
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-BIB-005", "ENF-SEC-010", "EF-USR-024")

EDITION = "01JEDITION00000000000000A"
SWITCH = "01JSWITCH000000000000000A"
JOB = "01JJOB0000000000000000000A"
KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
CLOSED = ("/api/v1/problems/credential-not-allowed", "/api/v1/problems/forbidden")
PREVIEW = f"/api/v1/editions/{EDITION}/team-switch"
APPLY = f"/api/v1/editions/{EDITION}/team-switch/{SWITCH}/apply"
REPORT = f"/api/v1/editions/{EDITION}/team-switch/{SWITCH}"
VIEW = {
    "id": SWITCH, "edition_id": EDITION, "team_id": "01JTEAM00000000000000000B",
    "state": "previewed", "to_switch": ["01JUNIT00000000000000000A"], "blocked": {},
    "pending": [], "switched": [], "created_at": "2026-11-17T12:00:00Z",
}  # fmt: skip


class _Service:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.claims: list[object] = []

    def preview(self, principal: Any, edition_id: str) -> dict[str, Any]:
        self.calls.append(("preview", edition_id))
        if edition_id != EDITION:
            raise TeamSwitchRefusedError("team_switch.not_found")
        return VIEW

    def queue(self, principal: Any, edition_id: str, switch_id: str) -> str:
        self.calls.append(("queue", edition_id, switch_id))
        if (edition_id, switch_id) != (EDITION, SWITCH):
            raise TeamSwitchRefusedError("team_switch.not_found")
        return JOB

    def report(self, principal: Any, edition_id: str, switch_id: str) -> dict[str, Any]:
        self.calls.append(("report", edition_id, switch_id))
        if (edition_id, switch_id) != (EDITION, SWITCH):
            raise TeamSwitchRefusedError("team_switch.not_found")
        return {**VIEW, "state": "done", "switched": VIEW["to_switch"], "to_switch": []}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[tuple[AuthEnv, _Service]]:
    service = _Service()
    with auth_env(monkeypatch, settings, ops_services=SimpleNamespace(team_switch=service)) as env:
        yield env, service


def test_preview_apply_and_report_by_a_fresh_admin_session(api: tuple[AuthEnv, _Service]) -> None:
    env, service = api
    env.login()
    preview = env.post(PREVIEW)
    assert preview.status_code == 201, preview.text
    assert preview.json()["id"] == SWITCH and preview.json()["to_switch"] == VIEW["to_switch"]
    applied = env.post(APPLY)
    assert applied.status_code == 202, applied.text
    assert applied.json() == {"job_id": JOB, "status_url": f"/api/v1/jobs/{JOB}"}
    assert applied.headers["location"] == f"/api/v1/jobs/{JOB}"
    report = env.client.get(REPORT)
    assert report.status_code == 200 and report.json()["state"] == "done"
    assert [c[0] for c in service.calls] == ["preview", "queue", "report"]


def _key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
    *extra: str,
) -> dict[str, str]:  # fmt: skip
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "k", *extra) == 0
    found = KEY.search(capsys.readouterr().out)
    assert found
    return {"Authorization": f"Bearer {found.group(0)}"}


DOORS = (("POST", PREVIEW), ("POST", APPLY), ("GET", REPORT))


def test_attacks_a_key_a_read_only_key_a_member_a_child_nobody_reach_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    api: tuple[AuthEnv, _Service],
) -> None:  # fmt: skip
    env, service = api
    keys = [
        _key(monkeypatch, capsys, env.settings),
        _key(monkeypatch, capsys, env.settings, "--read-only"),
    ]
    for method, path in DOORS:
        assert env.client.request(method, path).status_code in (401, 403), path
        for headers in keys:
            keyed = env.client.request(method, path, headers=headers)
            assert keyed.status_code == 403 and keyed.json()["type"] in CLOSED, (method, path)
    env.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(env, f"{role}1", role, ip, **extra)
        for method, path in DOORS:
            refused = who.request(method, path, headers={"X-CSRF-Token": env.csrf(who)})
            assert refused.status_code == 403, (role, method, path)
            assert JOB not in refused.text and SWITCH not in refused.text
    assert service.calls == []  # no refusal reached the service: nothing was prepared or queued


def test_a_stale_session_prepares_and_queues_nothing(api: tuple[AuthEnv, _Service]) -> None:
    env, service = api
    env.login()
    env.clock.advance(timedelta(minutes=20))
    for method, path in DOORS:
        stale = env.client.request(method, path)
        assert stale.status_code == 401, (method, path)
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert service.calls == []


def test_idor_somebody_elses_edition_or_switch_answers_like_an_unknown_one(
    api: tuple[AuthEnv, _Service],
) -> None:
    env, _service = api
    env.login()
    other_edition = "01JOTHER00000000000000000E"
    other_switch = "01JOTHER00000000000000000S"
    answers = [
        env.post(f"/api/v1/editions/{other_edition}/team-switch/{SWITCH}/apply"),
        env.post(f"/api/v1/editions/{EDITION}/team-switch/{other_switch}/apply"),
        env.client.get(f"/api/v1/editions/{other_edition}/team-switch/{SWITCH}"),
        env.client.get(f"/api/v1/editions/{EDITION}/team-switch/{other_switch}"),
        env.post(f"/api/v1/editions/{other_edition}/team-switch"),
    ]
    assert {a.status_code for a in answers} == {404}
    assert len({a.json()["type"] for a in answers}) == 1
    for answer in answers:  # the identifier asked for is not echoed
        assert other_edition not in answer.text and other_switch not in answer.text


def test_a_reserved_door_never_stores_the_body_digest_of_a_keyed_post(
    api: tuple[AuthEnv, _Service],
) -> None:
    env, service = api
    env.login()
    headers = {"Idempotency-Key": "-".join(["switch", "key", "0001", "abcdef"])}
    first = env.post(APPLY, headers=headers)
    second = env.post(APPLY, headers=headers)
    assert first.status_code == second.status_code == 202
    assert "idempotent-replayed" not in second.headers  # the guards run again, never a replay
    assert [c[0] for c in service.calls] == ["queue", "queue"]
