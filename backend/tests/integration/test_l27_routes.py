"""M3 L27 (EF-SRC-022, EF-BIB-054): the HTTP door of the identity migration of a source.

One door, the twin of `source migrate-ids`: a reserved action (a fresh admin session, never a key,
never a member or a child), preview and apply alike (the preview decrypts the tombstones, so it is
reserved too). The service is never called for a refusal. The attacks: a key (admin, read-only,
a child's), a member, a child, no session, a stale session, no confirmation, a wrong verb, an
unknown field, another source's plan."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env, run_cli
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("EF-SRC-022", "EF-BIB-054")

KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")
FP = "f" * 64
PATH = "/api/v1/sources/source-a:1/migrate-ids"
BODY: dict[str, Any] = {"from_scheme": "slug", "to_scheme": "numeric"}
REFUSED = ("/api/v1/problems/credential-not-allowed", "/api/v1/problems/forbidden")


class _SourceMigration:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def migrate_ids(
        self, source_key: str, *, from_scheme: str, to_scheme: str, confirm: str | None,
        principal: Any,
    ) -> dict[str, Any]:  # fmt: skip
        self.calls.append((source_key, from_scheme, to_scheme, confirm, principal.credential.value))
        return {"source_key": source_key, "from_scheme": from_scheme, "to_scheme": to_scheme,
                "migrate": [], "blocked": [], "unresolved": [], "fingerprint": FP,
                "applied": None if confirm is None else 0}  # fmt: skip


class _Ops:
    def __init__(self) -> None:
        self.source_migration = _SourceMigration()


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


def test_source_migrate_ids_door_refuses_key_member_child_anonymous_and_stale_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv, ops: _Ops
) -> None:
    admin_key = _key(monkeypatch, capsys, api.settings)
    scoped = _key(monkeypatch, capsys, api.settings, "--read-only")
    for body in (BODY, {**BODY, "confirm": FP}):
        assert api.client.post(PATH, json=body).status_code in (401, 403)  # no session
        for headers in (admin_key, scoped):  # not even a key with the admin scope
            refused = api.client.post(PATH, json=body, headers=headers)
            assert refused.status_code == 403
            assert refused.json()["type"] in REFUSED
    api.login()
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        extra: dict[str, object] = {"max_age_rating": 10} if role == "child" else {}
        who = add_profile(api, f"{role}1", role, ip, **extra)
        for body in (BODY, {**BODY, "confirm": FP}):  # the preview too: it opens the tombstones
            refused = who.post(PATH, json=body, headers={CSRF_HEADER: api.csrf(who)})
            assert refused.status_code == 403, (role, body)
            assert refused.json()["type"] == "/api/v1/problems/forbidden"
    api.clock.advance(timedelta(minutes=20))
    for body in (BODY, {**BODY, "confirm": FP}):
        stale = api.post(PATH, json=body)
        assert stale.status_code == 401
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert ops.source_migration.calls == []  # no refusal reached the service


def test_admin_session_previews_then_applies_with_the_fingerprint(api: AuthEnv, ops: _Ops) -> None:
    api.login()
    plan = api.post(PATH, json=BODY)
    assert plan.status_code == 200 and plan.json()["applied"] is None
    assert plan.json()["fingerprint"] == FP
    done = api.post(PATH, json={**BODY, "confirm": FP})
    assert done.status_code == 200 and done.json()["applied"] == 0
    assert [c[3] for c in ops.source_migration.calls] == [None, FP]
    assert {c[0] for c in ops.source_migration.calls} == {"source-a:1"}  # the path's source
    assert {c[4] for c in ops.source_migration.calls} == {"session"}


def test_the_door_refuses_what_it_cannot_read_before_the_service(api: AuthEnv, ops: _Ops) -> None:
    api.login()
    for body in (
        {"from_scheme": "slug"},  # no target scheme
        {**BODY, "to_scheme": "slug"},  # the same scheme: nothing to migrate
        {**BODY, "confirm": "x" * 500},
        {**BODY, "also": 1},  # unknown field
        {**BODY, "from_scheme": ""},
    ):
        assert api.post(PATH, json=body).status_code == 422, body
    assert ops.source_migration.calls == []
    # a verb that is not the door's
    for verb in ("get", "put", "delete", "patch"):
        assert getattr(api.client, verb)(PATH).status_code in (405, 401, 403)
