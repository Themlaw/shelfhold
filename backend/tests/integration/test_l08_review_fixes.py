"""M3 L08, answers to the review of #218 (EF-USR-013): what a deletion must reach, in any form.

The PoC of the reviewer are in `test_l08_review_poc.py` (untouched). These are the guards that
keep the same hole from reopening: every event field that cites a user is declared, every form of
audit target is covered, the tombstone waiver is rewritten once and nothing else, a ticket cannot
be written for a user that is gone, the export holds no other person's identifier.
"""

import ast
import json
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.events.bus import USER_FIELDS, EventBus, EventPayloadError, check_payload
from shelfhold.identity.application import profiles as profiles_module
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.sdk.refs import SeriesRef
from tests.auth_env import PASSWORD, AuthEnv, auth_env, db_rows
from tests.integration.test_user_delete_export import (
    PROFILES,
    _auth,
    _carrying,
    _id,
    _seed,
    _send,
    _sql,
)

SRC = Path(__file__).resolve().parents[2] / "src"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


# -- B1: every event field that cites a user is declared ------------------------------------------


@pytest.mark.req("EF-USR-013")
def test_an_event_field_that_names_a_user_must_be_declared_by_its_type() -> None:
    someone = new_ulid()
    check_payload({"user_id": someone, "passkey_id": new_ulid()}, "auth.passkey_registered")
    for key in ("user_id", "requester_user_id", "actor_id", "owner_id", "profile_id", "by"):
        with pytest.raises(EventPayloadError):
            check_payload({key: someone}, "ops.something_new")  # undeclared: refused
    with pytest.raises(EventPayloadError):
        check_payload({"user_id": someone}, "auth.other_type")  # declared for one type only
    check_payload({"request_id": someone, "work_id": someone}, "ops.request_arrived")  # not people
    assert all(fields for fields in USER_FIELDS.values())


@pytest.mark.req("EF-USR-013")
def test_the_events_of_every_declared_type_go_with_the_user(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    nan = _seed(api, "nan")
    engine = create_db_engine(api.settings.database_path)
    try:
        bus = EventBus(SystemClock())
        with engine.begin() as connection:
            for event_type, fields in USER_FIELDS.items():
                for owner in (mum, nan):
                    bus.publish(connection, event_type, {field: owner for field in fields})
    finally:
        engine.dispose()
    api.login()
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    assert _carrying(api, mum) == []
    still = db_rows(api.settings, "SELECT count(*) FROM event WHERE payload LIKE ?", f"%{nan}%")
    assert still == [(len(USER_FIELDS),)]  # her events only: nobody else's goes


# -- B2: every form of audit target is covered ---------------------------------------------------

_AUDITS = {"_audit", "_entry", "audit", "_record"}


def _target_forms() -> list[str]:
    """Every audit target written as an f-string in the code, with the user id where the code
    puts a value: the forms that the deletion has to un-name."""
    forms: list[str] = []
    for path in SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in ast.walk(tree):
            if not isinstance(call, ast.Call):
                continue
            name = getattr(call.func, "attr", getattr(call.func, "id", ""))
            if name not in _AUDITS:
                continue
            target = next((k.value for k in call.keywords if k.arg == "target"), None)
            if target is None and len(call.args) >= 3:
                target = call.args[2]
            if isinstance(target, ast.JoinedStr):
                forms.append(
                    "".join(
                        v.value if isinstance(v, ast.Constant) else "{ID}" for v in target.values
                    )
                )
    return forms


@pytest.mark.req("EF-USR-013", "ENF-OPS-009")
def test_every_form_of_audit_target_the_code_writes_loses_the_id() -> None:
    forms = _target_forms()
    assert any("user=" in form for form in forms)  # the form of the review (session.revoke_other)
    assert any("|" in form for form in forms) and len(forms) >= 5
    user_id = new_ulid()
    with closing(sqlite3.connect(":memory:")) as db:
        for form in forms:
            target = form.replace("{ID}", user_id)
            (after,) = db.execute(
                "SELECT replace(?, ?, 'deleted:0123456789abcdef')", (target, user_id)
            ).fetchone()
            assert user_id not in after, form


# -- B3 and m5: a private ticket stays private, nobody gets it back --------------------------------


@pytest.mark.req("EF-USR-013", "EF-USR-016")
def test_a_deleted_users_ticket_keeps_its_visibility_for_the_other_profiles(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    _sql(api, "UPDATE follow_request SET visibility = 'household' WHERE requester_user_id = ?", mum)
    other = _seed(api, "nan")
    api.login()
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    rows = db_rows(api.settings, "SELECT requester_user_id, visibility FROM follow_request")
    assert sorted(rows, key=str) == sorted([(None, "household"), (other, "private")], key=str)


# -- M2: a ticket cannot be written for a user that is gone ----------------------------------------


@pytest.mark.req("EF-USR-013")
def test_a_ticket_for_a_missing_user_is_written_for_nobody(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    ghost = new_ulid()
    engine = create_db_engine(api.settings.database_path)
    try:
        from shelfhold.acquisition.application.follow_request import FollowRequests

        follows = FollowRequests(engine, api.clock, lambda: None)
        follows._claim(
            SeriesRef(source_key="source-a", remote_id="late", title="x"), ghost, "private"
        )
        follows._claim(
            SeriesRef(source_key="source-a", remote_id="here", title="x"), mum, "private"
        )
    finally:
        engine.dispose()
    found = dict(
        db_rows(
            api.settings,
            "SELECT remote_id, requester_user_id FROM follow_request "
            "WHERE remote_id IN ('late', 'here')",
        )
    )
    assert found == {
        "late": None,
        "here": mum,
    }  # the one that exists is kept, the other is nobody's
    assert _carrying(api, ghost) == []


# -- M1 and the digest: documents that cite her as a text ----------------------------------


@pytest.mark.req("EF-USR-013")
def test_a_queued_digest_that_names_her_as_the_owner_of_an_item_takes_the_token(
    api: AuthEnv,
) -> None:
    mum = _seed(api, "mum")
    boss = _id(api, "admin")
    payload = json.dumps({"items": [{"owner_id": mum, "n": 1}, {"owner_id": boss, "n": 2}]})
    _sql(
        api,
        "INSERT INTO notification_digest (user_id, due_at, payload) VALUES (?, ?, ?)",
        boss, "2026-11-14 08:00:00", payload,
    )  # fmt: skip
    api.login()
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    (kept,) = db_rows(
        api.settings, "SELECT payload FROM notification_digest WHERE user_id = ?", boss
    )
    document = json.loads(str(kept[0]))  # still valid JSON
    owners = [item["owner_id"] for item in document["items"]]
    assert owners[1] == boss and owners[0].startswith("deleted:") and mum not in str(kept[0])


# -- M4: the waiver of a lost keyring is rewritten once, and nothing else ---------------


def _waiver(env: AuthEnv, actor: str) -> str:
    kid = f"kid-{new_ulid()[-8:].lower()}"
    _sql(env, "INSERT INTO tombstone_kid (kid, first_seq) VALUES (?, 1)", kid)
    _sql(env, "INSERT INTO tombstone_lost_kid (kid, actor_id) VALUES (?, ?)", kid, actor)
    return kid


@pytest.mark.req("EF-USR-013")
def test_the_waiver_names_the_deleted_admin_by_the_token(api: AuthEnv) -> None:
    boss = _auth(api).profile_add("boss", PASSWORD, "admin")
    kid = _waiver(api, boss)
    _auth(api).profile_remove(boss, by=None)
    ((actor,),) = db_rows(
        api.settings, "SELECT actor_id FROM tombstone_lost_kid WHERE kid = ?", kid
    )
    assert str(actor).startswith("deleted:")
    assert _carrying(api, boss) == []


@pytest.mark.req("EF-USR-013", "ENF-SEC-005")
def test_the_waiver_stays_append_only_but_for_the_one_rewrite(api: AuthEnv) -> None:
    kid = _waiver(api, "someone")
    path = api.settings.database_path

    def attempt(sql: str, *params: object) -> bool:
        with closing(sqlite3.connect(path)) as db:
            try:
                with db:
                    db.execute(sql, params)
            except sqlite3.DatabaseError:
                return False
            return True

    assert not attempt("UPDATE tombstone_lost_kid SET actor_id = 'x' WHERE kid = ?", kid)
    assert not attempt("UPDATE tombstone_lost_kid SET actor_id = NULL WHERE kid = ?", kid)
    assert not attempt(
        "UPDATE tombstone_lost_kid SET actor_id = 'deleted:ab', "
        "accepted_at = '2020-01-01 00:00:00' "
        "WHERE kid = ?",
        kid,
    )
    assert not attempt("DELETE FROM tombstone_lost_kid WHERE kid = ?", kid)
    assert attempt("UPDATE tombstone_lost_kid SET actor_id = 'deleted:ab' WHERE kid = ?", kid)
    # a token is not rewritten again, and no other value is taken afterwards
    assert not attempt("UPDATE tombstone_lost_kid SET actor_id = 'deleted:cd' WHERE kid = ?", kid)
    assert not attempt("UPDATE tombstone_lost_kid SET actor_id = 'someone' WHERE kid = ?", kid)


# -- M3, m2, m3: the export ------------------------------------------------------------


@pytest.mark.req("EF-USR-013")
def test_the_export_of_an_admin_names_no_other_profile_and_no_key_issued_for_a_child(
    api: AuthEnv,
) -> None:
    kid = _seed(api, "kid", "child", max_age_rating=6)
    admin = _id(api, "admin")
    api.login()  # the audit of the session now holds `profile.created <kid>`
    _sql(
        api,
        "INSERT INTO api_key (id, user_id, name, prefix, hash, scopes, subject_user_id) "
        "VALUES (?, ?, 'tablet of the child', 'ffff0001', ?, '[\"read\"]', ?)",
        new_ulid(), admin, "sha256:" + "ef" * 32, kid,
    )  # fmt: skip
    _sql(
        api,
        "INSERT INTO audit (id, actor_id, action, target) VALUES (?, ?, 'probe.child', ?)",
        new_ulid(), admin, f"user={kid} session={new_ulid()}",
    )  # fmt: skip
    exported = _send(api, "GET", f"{PROFILES}/{admin}/export")
    assert exported.status_code == 200
    assert kid not in exported.text and "tablet of the child" not in exported.text
    lines = [a for a in exported.json()["audit"] if a["action"] == "probe.child"]
    assert lines and "other-1" in lines[0]["target"] and "other-2" in lines[0]["target"]
    child = _send(api, "GET", f"{PROFILES}/{kid}/export").json()
    assert [k["prefix"] for k in child["api_keys"] if k["name"] == "tablet of the child"] == [
        "ffff0001"
    ]


@pytest.mark.req("EF-USR-013")
def test_the_export_names_the_sections_it_cut(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    mum = _seed(api, "mum")
    for _ in range(3):
        _sql(
            api,
            "INSERT INTO audit (id, actor_id, action, target) VALUES (?, ?, 'probe.acted', 'x')",
            new_ulid(), mum,
        )  # fmt: skip
    monkeypatch.setattr(profiles_module, "MAX_EXPORT_ROWS", 2)
    api.login()
    document = _send(api, "GET", f"{PROFILES}/{mum}/export").json()
    assert document["truncated"] == ["audit"] and len(document["audit"]) == 2
    monkeypatch.setattr(profiles_module, "MAX_EXPORT_ROWS", 10)
    assert _send(api, "GET", f"{PROFILES}/{mum}/export").json()["truncated"] == []
