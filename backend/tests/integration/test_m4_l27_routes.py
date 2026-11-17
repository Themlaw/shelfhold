"""M4 L27 (EF-BIB-014, EF-USR-024, ENF-SEC-010): the HTTP door of the migration of a series.

Four routes under one Edition: propose a numbering correspondence, read it, validate it, apply
it. All four are one reserved action, `source.migrate`: a fresh admin session, never a key (the
`admin` scope included, and a read-only one), never a device token, a member or a child. The
service is never reached for a refusal, and a refusal says nothing of which Editions or tables
exist (the same answer for an Edition that exists, one that does not, and another's table).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, run_cli
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-BIB-014", "ENF-SEC-010")

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
EDITION = "01HZZZZZZZZZZZZZZZZZZZZZZE"
OTHER_EDITION = "01HZZZZZZZZZZZZZZZZZZZZZZF"
TABLE = "01HZZZZZZZZZZZZZZZZZZZZZZT"
DIGEST = "d" * 64
REFUSED = ("/api/v1/problems/credential-not-allowed", "/api/v1/problems/forbidden")
BASE = "/api/v1/editions/{edition}/source-migrations"
PROPOSE_BODY: dict[str, Any] = {"source_key": "suwayomi:7002", "remote_id": "/new-source"}
VALIDATE_BODY: dict[str, Any] = {"digest": DIGEST, "overrides": [{"number": "2", "target": "1"}]}
APPLY_BODY: dict[str, Any] = {"digest": DIGEST}


def propose_path(edition: str = EDITION) -> str:
    return BASE.format(edition=edition)


def table_path(table: str = TABLE, edition: str = EDITION, tail: str = "") -> str:
    return f"{propose_path(edition)}/{table}{tail}"


def doors(
    edition: str = EDITION, table: str = TABLE
) -> list[tuple[str, str, dict[str, Any] | None]]:
    return [
        ("post", propose_path(edition), PROPOSE_BODY),
        ("get", table_path(table, edition), None),
        ("post", table_path(table, edition, "/validation"), VALIDATE_BODY),
        ("post", table_path(table, edition, "/application"), APPLY_BODY),
    ]


def proposal(**extra: Any) -> dict[str, Any]:
    return {
        "id": TABLE,
        "edition_id": EDITION,
        "state": "proposed",
        "digest": DIGEST,
        "offset": 0,
        "rows": [],
        "unmatched_units": [],
        "to_fetch": 0,
        "held_count": 0,
        "binding_id": None,
        **extra,
    }


class _Migration:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []  # (action, edition, credential)

    def _note(self, action: str, edition: str, principal: Any) -> dict[str, Any]:
        self.calls.append((action, edition, principal.credential.value))
        return proposal()

    def propose(self, edition_id: str, *, source_key: str, remote_id: str,
                replaces: str | None, principal: Any) -> dict[str, Any]:  # fmt: skip
        return self._note("propose", edition_id, principal)

    def get(self, edition_id: str, migration_id: str, *, principal: Any) -> dict[str, Any]:
        return self._note("get", edition_id, principal)

    def validate(self, edition_id: str, migration_id: str, *, digest: str,
                 overrides: list[tuple[str, str]], principal: Any) -> dict[str, Any]:  # fmt: skip
        return self._note("validate", edition_id, principal) | {"state": "validated"}

    def apply(self, edition_id: str, migration_id: str, *, digest: str,
              principal: Any) -> dict[str, Any]:  # fmt: skip
        return self._note("apply", edition_id, principal) | {"state": "applied"}


class _Ops:
    def __init__(self) -> None:
        self.series_migration = _Migration()


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


def _send(client: Any, verb: str, path: str, body: dict[str, Any] | None, **headers: str) -> Any:
    return getattr(client, verb)(path, **({"json": body} if body is not None else {}),
                                 headers=headers)  # fmt: skip


def test_source_migration_is_a_reserved_admin_action() -> None:
    """The action is declared once, with the effect that makes it fresh-required."""
    action = RESERVED_ACTIONS["source.migrate"]
    assert action.name == "source.migrate"
    assert action.needs_fresh_auth(restricted_profile_exists=False, hardened=False)


def test_source_migration_door_refuses_key_member_child_anonymous_and_stale_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv, ops: _Ops
) -> None:
    keys = {
        "admin": _key(monkeypatch, capsys, api.settings),
        "read-only": _key(monkeypatch, capsys, api.settings, "--read-only"),
    }
    for verb, path, body in doors():
        assert _send(api.client, verb, path, body).status_code in (401, 403)  # no session
        for name, headers in keys.items():  # not even a key with the admin scope
            refused = _send(api.client, verb, path, body, **headers)
            assert refused.status_code == 403, (name, verb, path)
            assert refused.json()["type"] in REFUSED
    api.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(api, f"{role}1", role, ip, **extra)
        for verb, path, body in doors():
            refused = _send(who, verb, path, body, **{CSRF_HEADER: api.csrf(who)})
            assert refused.status_code == 403, (role, verb, path)
            assert refused.json()["type"] == "/api/v1/problems/forbidden"
    api.clock.advance(timedelta(minutes=20))
    for verb, path, body in doors():
        stale = _send(api.client, verb, path, body, **{CSRF_HEADER: api.csrf()})
        assert stale.status_code == 401, (verb, path)
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert ops.series_migration.calls == []  # no refusal reached the service


def test_a_refusal_says_nothing_of_which_editions_and_tables_exist(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv, ops: _Ops
) -> None:
    """IDOR: a refused caller gets the same answer for an Edition that exists, one that does not,
    a table of another Edition and a table that was never made."""
    key = _key(monkeypatch, capsys, api.settings, "--read-only")
    api.login()
    member = add_profile(api, "mia", "member", "10.0.0.7")
    seen: set[tuple[int, str]] = set()
    for edition in (EDITION, OTHER_EDITION, "not-an-id"):
        for table in (TABLE, "01HZZZZZZZZZZZZZZZZZZZZZZU"):
            for verb, path, body in doors(edition, table):
                for who in (_send(member, verb, path, body, **{CSRF_HEADER: api.csrf(member)}),
                            _send(api.client, verb, path, body, **key)):  # fmt: skip
                    assert who.status_code in (403, 404, 405, 422), (verb, path)
                    if who.status_code == 403:
                        seen.add((who.status_code, who.json()["type"]))
    assert len(seen) <= 2  # forbidden for a session, credential-not-allowed for a key; no more
    assert ops.series_migration.calls == []


def test_admin_session_proposes_reads_validates_and_applies(api: AuthEnv, ops: _Ops) -> None:
    api.login()
    made = api.post(propose_path(), json=PROPOSE_BODY)
    assert made.status_code == 201 and made.json()["state"] == "proposed"
    assert api.client.get(table_path()).json()["digest"] == DIGEST
    valid = api.post(table_path(tail="/validation"), json=VALIDATE_BODY)
    assert valid.status_code == 200 and valid.json()["state"] == "validated"
    done = api.post(table_path(tail="/application"), json=APPLY_BODY)
    assert done.status_code == 200 and done.json()["state"] == "applied"
    assert [c[0] for c in ops.series_migration.calls] == ["propose", "get", "validate", "apply"]
    assert {c[1] for c in ops.series_migration.calls} == {EDITION}  # the path's Edition
    assert {c[2] for c in ops.series_migration.calls} == {"session"}


def test_the_door_refuses_what_it_cannot_read_before_the_service(api: AuthEnv, ops: _Ops) -> None:
    api.login()
    for path, body in (
        (propose_path(), {"source_key": "suwayomi:7002"}),  # no series
        (propose_path(), {**PROPOSE_BODY, "also": 1}),  # unknown field
        (propose_path(), {**PROPOSE_BODY, "remote_id": ""}),
        (propose_path(), {**PROPOSE_BODY, "remote_id": "x" * 2000}),
        (table_path(tail="/validation"), {"overrides": []}),  # no digest
        (table_path(tail="/validation"), {"digest": "short", "overrides": []}),
        (table_path(tail="/validation"), {**VALIDATE_BODY, "overrides": [{"number": "1"}]}),
        (table_path(tail="/validation"), {**VALIDATE_BODY, "overrides": [{"number": "1"}] * 5000}),
        (table_path(tail="/application"), {}),
        (table_path(tail="/application"), {"digest": DIGEST, "force": True}),
    ):
        assert api.post(path, json=body).status_code == 422, (path, body)
    assert api.post(table_path(), json={}).status_code in (404, 405)  # a verb that is not a door's
    for verb in ("put", "delete", "patch"):
        assert getattr(api.client, verb)(table_path()).status_code in (405, 401, 403)
    assert ops.series_migration.calls == []
