"""The removal of a destination with its files and the full detachment are background jobs
(M4 L34, EF-EXP-031, EF-INT-001, EF-USR-024).

The two doors answer `202 Accepted` with a `job_id` (and the `Location` of the job), like
`export.run`: the files go in the queue, which a pause stops. A removal that keeps the files and
the `give up` of an interrupted one stay synchronous: they touch the database only.

The doors are the real routes; the services behind them are a recorder. The attacks are the ones
of the other doors of L45: a key, a read-only key, a member, a child, nobody; the identifier of a
destination that does not exist is answered like one that does; the job is read by the admin only.
"""

import hashlib
from datetime import timedelta
import re
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.ops.errors import CodedRefusal
from tests.auth_env import AuthEnv, auth_env, db_rows, run_cli
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-EXP-031", "EF-INT-001", "EF-USR-024")

ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
DEST = "01JDEST000000000000000000A"
TARGET = "01JTARG000000000000000000A"
JOB = "01JJOB0000000000000000000A"
PHRASE = "remove --delete-files 01JDEST000000000000000000A"
KEEP_PHRASE = "remove --keep-files 01JDEST000000000000000000A"
DETACH_PHRASE = "detach 01JTARG000000000000000000A"
PASSWORD = "synthetic-kavita-pw"
REMOVAL = f"/api/v1/destinations/{DEST}/removal"
DETACH = f"/api/v1/targets/{TARGET}/detach"
REMOVAL_REPORT = {
    "deleted": 0, "forgotten": 3, "cancelled": 0, "complete": True, "left": [],
}  # fmt: skip
CLOSED = ("/api/v1/problems/credential-not-allowed", "/api/v1/problems/forbidden")


class _Teardown:
    """What the doors call. The queue methods give a job; the synchronous ones a report."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[tuple[Any, ...]] = []
        self.claims_seen: list[list[tuple[object, ...]]] = []  # `idempotency_key`, mid-request

    def _look(self) -> None:
        self.claims_seen.append(db_rows(self.settings, "SELECT * FROM idempotency_key"))

    def remove_queue(
        self, *, destination_id: str, mode: str, typed: str | None, principal: Any
    ) -> str:
        self._look()
        self.calls.append(("remove_queue", destination_id, mode, typed))
        if typed not in (PHRASE, KEEP_PHRASE):
            raise CodedRefusal("removal.refused", "confirmation_required")
        if destination_id != DEST:
            raise CodedRefusal("removal.refused", "unknown_destination")
        return JOB

    def remove(
        self, *, destination_id: str, mode: str, typed: str | None, give_up: bool, principal: Any
    ) -> dict[str, Any]:
        self._look()
        self.calls.append(("remove", destination_id, mode, typed, give_up))
        return REMOVAL_REPORT

    def full_queue(
        self, *, target_id: str, typed: str | None, credential: str | None,
        discard_hidden_data: bool, discard_tombstone_journal: bool, key_gone: bool,
        principal: Any,
    ) -> str:  # fmt: skip
        self._look()
        self.calls.append(("full_queue", target_id, typed, credential, key_gone))
        if typed != DETACH_PHRASE:
            raise CodedRefusal("detach.refused", "confirmation_required")
        return JOB

    def full(self, **kwargs: Any) -> dict[str, Any]:
        self._look()
        self.calls.append(("full", kwargs["target_id"], kwargs["give_up"]))
        return {"target_id": kwargs["target_id"], "complete": True, "cancelled": 0}


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[tuple[AuthEnv, _Teardown]]:
    teardown = _Teardown(settings)
    with auth_env(monkeypatch, settings, ops_services=SimpleNamespace(teardown=teardown)) as env:
        yield env, teardown


def test_remove_with_files_returns_202_and_job_id(api: tuple[AuthEnv, _Teardown]) -> None:
    env, teardown = api
    env.login()
    response = env.post(REMOVAL, json={"mode": "delete_files", "typed": PHRASE})
    assert response.status_code == 202, response.text
    body = response.json()
    assert body == {"job_id": JOB, "status_url": f"/api/v1/jobs/{JOB}"}
    assert response.headers["location"] == f"/api/v1/jobs/{JOB}"
    assert teardown.calls == [("remove_queue", DEST, "delete_files", PHRASE)]  # no report: a job
    assert "deleted" not in response.text and PHRASE not in response.text


def test_remove_keeping_the_files_and_giving_up_stay_synchronous(
    api: tuple[AuthEnv, _Teardown],
) -> None:
    env, teardown = api
    env.login()
    kept = env.post(REMOVAL, json={"mode": "keep_files", "typed": KEEP_PHRASE})
    assert kept.status_code == 200 and kept.json() == REMOVAL_REPORT
    gave_up = env.post(REMOVAL, json={"mode": "delete_files", "typed": "x", "give_up": True})
    assert gave_up.status_code == 200 and gave_up.json() == REMOVAL_REPORT
    assert [c[0] for c in teardown.calls] == ["remove", "remove"]  # nothing was queued


def test_detach_returns_202_and_job_id_and_never_echoes_the_password(
    api: tuple[AuthEnv, _Teardown],
) -> None:
    env, teardown = api
    env.login()
    body = {"typed": DETACH_PHRASE, "credential": PASSWORD, "key_gone": True}
    response = env.post(DETACH, json=body)
    assert response.status_code == 202, response.text
    assert response.json() == {"job_id": JOB, "status_url": f"/api/v1/jobs/{JOB}"}
    assert response.headers["location"] == f"/api/v1/jobs/{JOB}"
    assert teardown.calls == [("full_queue", TARGET, DETACH_PHRASE, PASSWORD, True)]
    assert PASSWORD not in response.text and PASSWORD not in str(response.headers)
    given_up = env.post(DETACH, json={"typed": "x", "give_up": True})
    assert given_up.status_code == 200 and teardown.calls[-1] == ("full", TARGET, True)


def test_a_wrong_phrase_queues_nothing_and_an_unknown_destination_looks_like_a_known_one(
    api: tuple[AuthEnv, _Teardown],
) -> None:
    env, teardown = api
    env.login()
    wrong = env.post(REMOVAL, json={"mode": "delete_files", "typed": PHRASE + " "})
    assert wrong.status_code == 409 and wrong.json()["type"].endswith("removal.refused")
    other = "01JOTHER00000000000000000A"
    refused = env.post(
        f"/api/v1/destinations/{other}/removal", json={"mode": "delete_files", "typed": "x"}
    )
    assert refused.status_code == 409 and refused.json()["type"] == wrong.json()["type"]
    assert other not in refused.text  # the identifier asked for is not echoed


def _key(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
         *extra: str) -> dict[str, str]:  # fmt: skip
    capsys.readouterr()
    assert run_cli(monkeypatch, settings, "admin", "keys", "create", "--name", "k", *extra) == 0
    found = KEY.search(capsys.readouterr().out)
    assert found
    return {"Authorization": f"Bearer {found.group(0)}"}


DOORS = (
    ("POST", REMOVAL, {"mode": "delete_files", "typed": PHRASE}),
    ("POST", DETACH, {"typed": DETACH_PHRASE, "credential": PASSWORD}),
)


def test_attacks_a_key_a_read_only_key_a_member_a_child_nobody_queue_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    api: tuple[AuthEnv, _Teardown],
) -> None:  # fmt: skip
    env, teardown = api
    keys = [_key(monkeypatch, capsys, env.settings), _key(monkeypatch, capsys, env.settings,
                                                          "--read-only")]  # fmt: skip
    for method, path, body in DOORS:
        assert env.client.request(method, path, json=body).status_code in (401, 403), path
        for headers in keys:
            keyed = env.client.request(method, path, json=body, headers=headers)
            assert keyed.status_code == 403 and keyed.json()["type"] in CLOSED, path
    env.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(env, f"{role}1", role, ip, **extra)
        for method, path, body in DOORS:
            refused = who.request(method, path, json=body, headers={"X-CSRF-Token": env.csrf(who)})
            assert refused.status_code == 403, (role, path)
            assert JOB not in refused.text and "job_id" not in refused.text
    assert teardown.calls == []  # no refusal reached a service, so no job was queued


def test_a_stale_session_queues_nothing(api: tuple[AuthEnv, _Teardown]) -> None:

    env, teardown = api
    env.login()
    env.clock.advance(timedelta(minutes=20))
    for _method, path, body in DOORS:
        stale = env.post(path, json=body)
        assert stale.status_code == 401
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert teardown.calls == []


def test_the_job_of_a_removal_is_read_by_the_admin_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    api: tuple[AuthEnv, _Teardown],
) -> None:  # fmt: skip
    env, _teardown = api
    env.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(env, f"{role}2", role, ip, **extra)
        assert who.get(f"/api/v1/jobs/{JOB}").status_code in (401, 403, 404), role


def _claim_digest(body: dict[str, Any], path: str) -> str:
    """What the middleware kept of a keyed request before L34: the digest of the query, the
    content type and the body (the phrase, the Kavita password)."""
    import json

    raw = json.dumps(body).encode()
    h = hashlib.sha256()
    for part in (b"", b"application/json", raw):
        h.update(len(part).to_bytes(8, "big") + part)
    return f"sha256:{h.hexdigest()}"


def test_reserved_route_never_stores_body_digest(api: tuple[AuthEnv, _Teardown]) -> None:
    """A keyed POST on a reserved route leaves nothing in `idempotency_key`: no row while the
    request runs (a kill -9 mid-request would leave it), no row after. A digest of the body of
    the detachment is a digest of a password."""
    env, teardown = api
    env.login()
    sent = 0
    for path, body in (
        (REMOVAL, {"mode": "delete_files", "typed": PHRASE}),
        (DETACH, {"typed": DETACH_PHRASE, "credential": PASSWORD}),
        (REMOVAL, {"mode": "delete_files", "typed": PHRASE + " "}),  # refused: 409
        (DETACH, {"typed": "wrong", "credential": PASSWORD}),  # refused: 409
        (f"/api/v1/destinations/{DEST}/removal-preview", {"mode": "delete_files"}),
    ):
        env.post(path, json=body, headers={"Idempotency-Key": f"key-{sent:04d}-abcdef"})
        sent += 1
        digest = _claim_digest(body, path)
        for seen in teardown.claims_seen:
            assert seen == [], f"a claim of a reserved route was in the table: {seen}"
        assert digest not in str(db_rows(env.settings, "SELECT * FROM idempotency_key"))
    assert db_rows(env.settings, "SELECT COUNT(*) FROM idempotency_key") == [(0,)]


def test_the_same_key_twice_on_a_reserved_route_is_never_a_replay(
    api: tuple[AuthEnv, _Teardown],
) -> None:
    env, teardown = api
    env.login()
    headers = {"Idempotency-Key": "-".join(["same", "key", "0001", "abcdef"])}
    body = {"mode": "delete_files", "typed": PHRASE}
    first = env.post(REMOVAL, json=body, headers=headers)
    second = env.post(REMOVAL, json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert "idempotent-replayed" not in second.headers  # the second goes through the guards again
    assert [c[0] for c in teardown.calls] == ["remove_queue", "remove_queue"]


def test_retrying_a_dead_removal_or_detach_job_is_reserved_a_key_is_refused(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings,
) -> None:  # fmt: skip
    """`jobs retry` brings a job back from the dead. For a removal or a detachment that is an
    order the session did not give, so a key is refused, as for the application of a rename plan."""
    retried: list[str] = []
    ops = SimpleNamespace(
        teardown=_Teardown(settings),
        jobs_retry_reserved_action=lambda _id: "destination.remove_all",
        jobs_retry=lambda job_id, principal=None: retried.append(job_id),
    )
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        key = _key(monkeypatch, capsys, settings)
        url = f"/api/v1/jobs/{JOB}/retry"
        refused = env.client.post(url, headers=key)
        assert refused.status_code == 403 and refused.json()["type"] in CLOSED
        env.login()
        env.clock.advance(timedelta(minutes=20))
        assert env.post(url).status_code == 401  # a stale session: asked to prove itself again
        assert retried == []
