"""Contradicteur de la #218 (M3 L08, EF-USR-013) : des traces qui survivent à la suppression.

Each case is a real path of the code (the HTTP application, the service, or the very call the
production makes, quoted by file) followed by a deletion. EF-USR-013: "plus aucune ligne hors
audit ne porte son `user_id` et les entrées d'audit portent un jeton opaque". Red until fixed,
except `test_download_ticket_is_unnamed_on_deletion`, the missing guard of a branch of `_unname`
that only the code covered (green: it pins the behaviour).
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.acquisition.application.follow_request import FollowRequests
from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.jobs.control import QueueControl
from shelfhold.sdk.refs import SeriesRef
from tests.auth_env import PASSWORD, AuthEnv, auth_env, db_rows
from tests.integration.test_user_delete_export import (
    PROFILES,
    TITLE,
    WORK,
    _as,
    _auth,
    _carrying,
    _seed,
    _send,
    _sql,
)

ACTIVITY = "/api/v1/activity"
ADMIN_SESSIONS = "/api/v1/admin/sessions"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _audit_naming(env: AuthEnv, user_id: str) -> list[tuple[Any, ...]]:
    return db_rows(
        env.settings,
        "SELECT action, actor_id, target FROM audit WHERE actor_id LIKE ? OR target LIKE ?",
        f"%{user_id}%", f"%{user_id}%",
    )  # fmt: skip


# -- B1: the payload of an event names her (the decision's point 5 says no known type does) -----


@pytest.mark.req("EF-USR-013")
def test_poc_passkey_registered_event_keeps_the_deleted_users_id(api: AuthEnv) -> None:
    """`main.py` `passkey_registered`: `bus.publish(connection, "auth.passkey_registered",
    {"user_id": record.user_id, "passkey_id": record.id})`, no `actor_id`, visibility `ops`.
    `_unname` erases the events whose ACTOR is her; this one has no actor and survives 90 days."""
    mum = _seed(api, "mum")
    _sql(
        api,
        "INSERT INTO event (type, schema_version, payload, occurred_at, actor_id, visibility) "
        "VALUES ('auth.passkey_registered', 1, ?, '2026-11-12 10:00:00', NULL, 'ops')",
        json.dumps({"user_id": mum, "passkey_id": new_ulid()}),
    )  # fmt: skip
    api.login()
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    assert _carrying(api, mum) == []  # red: ['event.payload']


# -- B2: an audit target that names her in the middle -------------------------------------------


@pytest.mark.req("EF-USR-013", "ENF-OPS-009")
def test_poc_revoke_other_audit_target_keeps_the_deleted_users_id(api: AuthEnv) -> None:
    """`session_revoke_of_other` audits `target=f"user={owner} session={session_id}"`: neither
    `target == id` nor `target LIKE 'id|%'` reaches it. Real path: the admin closes her session,
    then removes her."""
    mum = _seed(api, "mum")
    _as(api, "mum", "127.0.0.2")
    api.login()
    sessions = api.client.get(ADMIN_SESSIONS).json()["items"]
    (hers,) = [s["id"] for s in sessions if s["user_id"] == mum]
    assert _send(api, "DELETE", f"{ADMIN_SESSIONS}/{hers}").status_code == 204
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    assert _audit_naming(api, mum) == []  # red: session.revoke_other, target 'user=<id> ...'


# -- M1: a pause of the queue keeps the admin who paused it -------------------------------------


@pytest.mark.req("EF-USR-013")
def test_poc_queue_pause_keeps_the_deleted_admins_id(api: AuthEnv) -> None:
    """`ops/maintenance.py` `pause`: `self.control.pause(conn, ..., by=_user(principal))` writes
    `{"since": ..., "by": <user_id>}` in `app_setting`, a JSON with no foreign key. A pause of one
    source may last months."""
    boss = _auth(api).profile_add("boss", PASSWORD, "admin")
    engine = create_db_engine(api.settings.database_path)
    try:
        with engine.begin() as conn:
            QueueControl().pause(conn, source="source-a", at=api.clock.now(), by=boss)
    finally:
        engine.dispose()
    _auth(api).profile_remove(boss, by=None)
    assert _carrying(api, boss) == []  # red: ['app_setting.value']


# -- M2: the race: a ticket written after the deletion carries her id for ever ------------------


@pytest.mark.req("EF-USR-013")
def test_poc_follow_claim_after_her_deletion_writes_her_id(api: AuthEnv) -> None:
    """Her request was authenticated, then the admin deleted her, then the claim commits:
    `FollowRequests._claim` inserts `requester_user_id` (no foreign key) without checking that the
    user still exists in its BEGIN IMMEDIATE. `_unname` already ran: nothing ever clears it."""
    mum = _seed(api, "mum")
    _sql(api, "DELETE FROM follow_request")
    _auth(api).profile_remove(mum, by=None)  # the deletion lands between auth and claim
    engine = create_db_engine(api.settings.database_path)
    try:
        follows = FollowRequests(engine, api.clock, lambda: None)
        follows._claim(
            SeriesRef(source_key="source-a", remote_id="series-late", title="x"), mum, "private"
        )
    finally:
        engine.dispose()
    assert _carrying(api, mum) == []  # red: ['follow_request.requester_user_id']


# -- B3: `ops` unmasks a private ticket of a deleted adult, without the reserved action ---------


def _ticket(env: AuthEnv, user_id: str) -> str:
    edition = new_ulid()
    _sql(
        env,
        "INSERT INTO edition (id, work_id, lang, translation_type, media) "
        "VALUES (?, ?, 'fr', 'official', 'images')",
        edition, WORK,
    )  # fmt: skip
    ticket = new_ulid()
    _sql(
        env,
        "INSERT INTO download_request (id, target_kind, range_ref, source_binding_id, state, "
        "requested_at, visibility, requester_user_id) "
        "VALUES (?, 'range', ?, 'x', 'requested', '2026-11-05 09:00:00', 'private', ?)",
        ticket, json.dumps({"edition_id": edition}), user_id,
    )  # fmt: skip
    return ticket


def _seen_by_admin(env: AuthEnv, ticket: str) -> dict[str, Any]:
    items = env.client.get(ACTIVITY).json()
    items = items["items"] if isinstance(items, dict) else items
    (item,) = [i for i in items if str(i["event_id"]) == ticket]
    return dict(item)


@pytest.mark.req("EF-USR-013", "EF-USR-016")
def test_poc_deletion_unmasks_her_private_ticket_for_the_admin(api: AuthEnv) -> None:
    """A private ticket of an adult is masked for the admin; unmasking is a reserved, audited
    action (`request.unmask`). `_unname` turns it into `ops`, which the admin sees with its title:
    the deletion reveals what she kept private, with no unmask and no audit line."""
    mum = _seed(api, "mum")
    ticket = _ticket(api, mum)
    api.login()
    before = _seen_by_admin(api, ticket)
    assert before["title_masked"] is True and before["title"] is None
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    after = _seen_by_admin(api, ticket)
    assert after["title"] != TITLE  # red: the title she kept private is shown
    assert after["title_masked"] is True


# -- guard: the download ticket branch of `_unname` (only the code covered it) ------------------


@pytest.mark.req("EF-USR-013")
def test_download_ticket_is_unnamed_on_deletion(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    ticket = _ticket(api, mum)
    api.login()
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    assert db_rows(
        api.settings,
        "SELECT requester_user_id FROM download_request WHERE id = ?", ticket,
    ) == [(None,)]  # fmt: skip
    assert _carrying(api, mum) == []


# -- M3: the export misses her own tickets ------------------------------------------------------


@pytest.mark.req("EF-USR-013")
def test_poc_export_holds_her_follow_and_download_tickets(api: AuthEnv) -> None:
    """EF-USR-013: "l'export JSON contient ses données". Her follow and download tickets carry
    her id (`_unname` has to un-name them, so they are hers), yet the export reads neither."""
    mum = _seed(api, "mum")
    ticket = _ticket(api, mum)
    (follow,) = db_rows(
        api.settings, "SELECT id FROM follow_request WHERE requester_user_id = ?", mum
    )
    api.login()
    exported = _send(api, "GET", f"{PROFILES}/{mum}/export")
    assert exported.status_code == 200
    assert str(follow[0]) in exported.text  # red
    assert ticket in exported.text  # red
