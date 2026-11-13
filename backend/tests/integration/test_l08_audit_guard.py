"""M3 L08 (EF-USR-013): an audit line written after a deletion never names the deleted user.

A session is cut in the middle of an action: the principal was authenticated, an admin deletes the
user, then the action writes its audit line. `_unname` already ran, so the line must not put the id
back (the one-year expiry of the audit is not enough for the right to be forgotten)."""

import re
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.headless import records
from shelfhold.identity.application.ports import AuditEntry
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from tests.auth_env import AuthEnv, auth_env, db_rows
from tests.integration.test_user_delete_export import PROFILES, _auth, _carrying, _id, _seed, _send

TOKEN = re.compile(r"deleted:[0-9a-f]{16}")


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _principal(user_id: str) -> Principal:
    return Principal(
        user_id=user_id, username="mum", role="member", credential=Credential.SESSION,
        session_id=new_ulid(),
    )  # fmt: skip


def _store(env: AuthEnv) -> Any:
    return _auth(env)._store


@pytest.mark.req("EF-USR-013", "ENF-OPS-009")
def test_a_session_cut_midway_writes_no_audit_line_that_names_the_deleted_user(
    api: AuthEnv,
) -> None:
    mum = _seed(api, "mum")
    principal = _principal(mum)  # authenticated before the deletion
    api.login()
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    # every door that writes the audit of the identity service, after the deletion
    _auth(api)._audit("probe.service", principal, target="x")
    _store(api).audit(AuditEntry(actor_id=mum, action="probe.store", at=api.clock.now()))
    engine = create_db_engine(api.settings.database_path)
    try:
        with engine.begin() as connection:
            records.insert_audit(
                connection, audit_id=new_ulid(), actor_id=mum, action="probe.headless",
                target="x", at=api.clock.now(), session_id=None, auth_time=None,
            )  # fmt: skip
    finally:
        engine.dispose()
    lines = db_rows(api.settings, "SELECT action, actor_id FROM audit WHERE action LIKE 'probe.%'")
    assert sorted(str(a) for a, _ in lines) == ["probe.headless", "probe.service", "probe.store"]
    assert all(TOKEN.fullmatch(str(actor)) for _, actor in lines), lines  # opaque, kept, not her id
    assert len({actor for _, actor in lines}) == 3  # nothing links them to each other or to her
    assert db_rows(api.settings, "SELECT count(*) FROM audit WHERE instr(actor_id, ?) > 0", mum)[
        0
    ] == (0,)
    assert _carrying(api, mum) == []


@pytest.mark.req("EF-USR-013")
def test_a_live_user_and_a_label_are_written_as_they_are(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    boss = _id(api, "admin")
    _store(api).audit(AuditEntry(actor_id=mum, action="probe.live", at=api.clock.now()))
    _store(api).audit(
        AuditEntry(actor_id=f"session:{new_ulid()}", action="probe.label", at=api.clock.now())
    )
    _store(api).audit(AuditEntry(actor_id=None, action="probe.none", at=api.clock.now()))
    rows = {
        a: actor
        for a, actor in db_rows(
            api.settings, "SELECT action, actor_id FROM audit WHERE action LIKE 'probe.%'"
        )
    }
    assert rows["probe.live"] == mum and rows["probe.none"] is None
    assert str(rows["probe.label"]).startswith("session:")
    assert boss  # the admin of the fixture exists
