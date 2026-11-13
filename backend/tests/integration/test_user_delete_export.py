"""M3 L08 (EF-USR-013, EF-USR-024, ENF-OPS-009, ENF-SEC-010): deleting a user and exporting one.

Deleting goes through the real application (`create_app()`, real sessions, real CSRF), the
service and the host CLI. After a deletion no row outside the audit carries the user's id; the
audit keeps its lines under one opaque token. Exporting reveals one person's data, so it is a
reserved action like the deletion: an admin session, fresh, never a key, never the person
herself nor a child, and the same answer for an id that exists and one that does not.
"""

import json
import re
import secrets
import sqlite3
from collections.abc import Iterator
from contextlib import closing
from datetime import timedelta
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS, LocalAuth, ProfileError
from shelfhold.infrastructure.ids import new_ulid
from tests.auth_env import ADMIN, CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows, run_cli

PROFILES = "/api/v1/profiles"
KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")
WORK = "01HZZZZZZZZZZZZZZZZZZZZZZA"
HIDDEN_WORK = "01HZZZZZZZZZZZZZZZZZZZZZZB"
TITLE = "Tale of Orchard"
HIDDEN_TITLE = "Tale of Hidden Lantern"
SECRET_HASH = "$argon2id$leakmarker"  # noqa: S105 (test fixture)
KEY_HASH = "sha256:" + "ab" * 32
TOKEN_HASH = "sha256:" + "cd" * 32
EXPORT_KEYS = {
    "format", "version", "profile", "preferences", "source_policies", "user_state", "requests",
    "reading_positions", "sessions", "api_keys", "passkeys", "audit", "follow_requests",
    "download_requests", "notification_digests", "truncated",
}  # fmt: skip


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


def _auth(env: AuthEnv) -> LocalAuth:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined,no-any-return]


def _sql(env: AuthEnv, sql: str, *params: object) -> None:
    with closing(sqlite3.connect(env.settings.database_path)) as connection, connection:
        connection.execute(sql, params)


def _id(env: AuthEnv, username: str) -> str:
    return str(db_rows(env.settings, 'SELECT id FROM "user" WHERE username = ?', username)[0][0])


def _count(env: AuthEnv, sql: str, *params: object) -> int:
    return int(db_rows(env.settings, sql, *params)[0][0])  # type: ignore[call-overload]


def _send(env: AuthEnv, method: str, path: str, client: Any = None) -> Any:
    client = client or env.client
    return client.request(method, path, headers={CSRF_HEADER: env.csrf(client)})


def _as(env: AuthEnv, username: str, ip: str) -> Any:
    client = env.other_client(ip)
    assert env.login(username, client=client).status_code == 200
    return client


def _seed(env: AuthEnv, username: str, role: str = "member", **kw: Any) -> str:
    """A profile that holds something in every place that names a user."""
    user_id = _auth(env).profile_add(username, PASSWORD, role, **kw)
    kind = str(db_rows(env.settings, "SELECT key FROM work_kind LIMIT 1")[0][0])
    for work_id, title in ((WORK, TITLE), (HIDDEN_WORK, HIDDEN_TITLE)):
        _sql(
            env,
            "INSERT OR IGNORE INTO work (id, kind, title, sort_title, age_rating, age_x, rated_at) "
            "VALUES (?, ?, ?, ?, 6, 0, '2026-11-01 10:00:00')",
            work_id, kind, title, title.lower(),
        )  # fmt: skip
    _sql(
        env,
        "INSERT INTO user_state (user_id, work_id, followed, favorite) VALUES (?, ?, 1, 1)",
        user_id, WORK,
    )  # fmt: skip
    _sql(
        env,
        "INSERT INTO request (id, requester_user_id, target_kind, target_value, state, "
        "decided_by, shown_title) VALUES (?, ?, 'work_id', ?, 'approved', ?, ?)",
        new_ulid(), user_id, HIDDEN_WORK,
        _id(env, ADMIN), HIDDEN_TITLE,
    )  # fmt: skip
    _sql(
        env,
        "INSERT INTO api_key (id, user_id, name, prefix, hash, scopes, subject_user_id) "
        "VALUES (?, ?, 'device', ?, ?, '[\"read\"]', ?)",
        new_ulid(), user_id, secrets.token_hex(4), KEY_HASH, user_id,
    )  # fmt: skip
    _sql(
        env,
        "INSERT INTO follow_request (id, source_key, remote_id, state, visibility, "
        "requester_user_id) VALUES (?, 'source-a', ?, 'approved', 'private', ?)",
        new_ulid(), f"series-{username}", user_id,
    )  # fmt: skip
    if role != "child":  # a child has no policy off a family source
        _auth(env).profile_policy_set(user_id, "source-a", "request")
    return user_id


def _carrying(env: AuthEnv, user_id: str) -> list[str]:
    """Every `table.column` of the database, but the audit, that holds the id as a value."""
    found: list[str] = []
    with closing(sqlite3.connect(env.settings.database_path)) as db:
        tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        for table in tables:
            if table == "audit" or table.startswith("sqlite_"):
                continue
            for column in db.execute(f'PRAGMA table_info("{table}")').fetchall():
                name, kind = column[1], str(column[2]).upper()
                if kind not in ("", "TEXT", "VARCHAR", "VARCHAR(26)", "CHAR(26)", "JSON"):
                    continue
                hit = db.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE CAST("{name}" AS TEXT) LIKE ?',  # noqa: S608 (names read from sqlite_master)
                    (f"%{user_id}%",),
                ).fetchone()[0]
                if hit:
                    found.append(f"{table}.{name}")
    return found


# -- delete: cascade and export ----------------------------------------------------------------


@pytest.mark.req("EF-USR-013")
def test_delete_user_cascade_and_export_json(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    _as(api, "mum", "127.0.0.2")  # a live session of hers
    _sql(api, "UPDATE session SET token_hash = ? WHERE user_id = ?", TOKEN_HASH, mum)
    _sql(api, 'UPDATE "user" SET password_hash = ? WHERE id = ?', SECRET_HASH, mum)
    assert _carrying(api, mum)  # the premise: she is named in many places
    api.login()
    exported = _send(api, "GET", f"{PROFILES}/{mum}/export")
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("application/json")
    assert "attachment" in exported.headers.get("content-disposition", "")
    assert exported.headers.get("cache-control") == "no-store"
    document = exported.json()
    assert set(document) == EXPORT_KEYS
    assert document["profile"]["username"] == "mum"
    assert [s["source_key"] for s in document["source_policies"]] == ["source-a"]
    assert [s["work_id"] for s in document["user_state"]] == [WORK]
    assert [r["target_value"] for r in document["requests"]] == [HIDDEN_WORK]
    assert [k["prefix"] for k in document["api_keys"]] == [
        r[0] for r in db_rows(api.settings, "SELECT prefix FROM api_key WHERE user_id = ?", mum)
    ]
    assert document["sessions"]
    # what proves or opens something never leaves, nor the titles that she may not see
    text = exported.text
    for secret in (SECRET_HASH, KEY_HASH, TOKEN_HASH, HIDDEN_TITLE):
        assert secret not in text
    assert "password" not in text.lower() and "csrf" not in text.lower()
    # the deletion
    gone = _send(api, "DELETE", f"{PROFILES}/{mum}")
    assert gone.status_code == 204
    assert _carrying(api, mum) == []
    assert _count(api, 'SELECT COUNT(*) FROM "user" WHERE id = ?', mum) == 0
    assert _count(api, "SELECT COUNT(*) FROM work WHERE id = ?", WORK) == 1  # the library stays
    # twice: she is gone like an id that never existed
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 404
    assert _send(api, "GET", f"{PROFILES}/{mum}/export").status_code == 404


@pytest.mark.req("EF-USR-013", "ENF-SEC-010")
def test_a_deleted_users_session_and_key_open_nothing_any_more(api: AuthEnv) -> None:
    kid = _seed(api, "kid", "child", max_age_rating=6)
    session = _as(api, "kid", "127.0.0.2")
    assert session.get("/api/v1/me/visibility").status_code == 200
    api.login()
    assert _send(api, "DELETE", f"{PROFILES}/{kid}").status_code == 204
    assert session.get("/api/v1/me/visibility").status_code == 401
    assert _count(api, "SELECT COUNT(*) FROM session WHERE user_id = ?", kid) == 0
    assert _count(api, "SELECT COUNT(*) FROM api_key WHERE subject_user_id = ?", kid) == 0


@pytest.mark.req("EF-USR-013", "EF-USR-016", "ENF-SEC-010")
def test_a_deleted_users_tickets_never_become_visible_to_the_household(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    api.login()
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    # the follow request stays (the admin still has to see its outcome) but belongs to nobody. Its
    # visibility is NOT widened (review of #218, B3): still `private`, never `ops` or `household`
    assert db_rows(api.settings, "SELECT requester_user_id, visibility FROM follow_request") == [
        (None, "private")
    ]
    assert db_rows(api.settings, "SELECT decided_by FROM request") == []  # the request went too


@pytest.mark.req("EF-USR-013")
def test_a_decision_taken_by_a_deleted_admin_names_nobody(api: AuthEnv) -> None:
    boss = _auth(api).profile_add("boss", PASSWORD, "admin")
    mum = _seed(api, "mum")
    _sql(api, "UPDATE request SET decided_by = ?", boss)
    _auth(api).profile_remove(boss, by=None)
    assert db_rows(api.settings, "SELECT decided_by FROM request WHERE requester_user_id = ?", mum)
    assert _carrying(api, boss) == []
    (decided,) = db_rows(api.settings, "SELECT decided_by FROM request")[0]
    assert decided is None or str(decided).startswith("deleted:")


# -- delete: the audit keeps one opaque token ----------------------------------------------------


@pytest.mark.req("EF-USR-013", "ENF-OPS-009")
def test_delete_user_pseudonymizes_audit(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    _as(api, "mum", "127.0.0.2")
    api.login()
    _sql(
        api,
        "INSERT INTO audit (id, actor_id, action, target) VALUES (?, ?, 'probe.acted', 'x')",
        new_ulid(), mum,
    )  # fmt: skip
    _sql(
        api,
        "INSERT INTO audit (id, actor_id, action, target) VALUES (?, ?, 'probe.targeted', ?)",
        new_ulid(), _id(api, ADMIN), f"{mum}|source-a",
    )  # fmt: skip
    _sql(
        api,
        "INSERT INTO event (type, schema_version, payload, occurred_at, actor_id, visibility) "
        "VALUES ('probe.event', 1, '{}', '2026-11-10 10:00:00', ?, 'household')",
        mum,
    )  # fmt: skip
    before = _count(api, "SELECT COUNT(*) FROM audit")
    floor = _count(api, "SELECT events_purged_through FROM instance")
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    # no line goes: they keep their action, their order and their place, under one token
    assert (
        _count(api, "SELECT COUNT(*) FROM audit") == before + 2
    )  # + the reserved action and `profile.removed`
    assert (
        _count(
            api, "SELECT COUNT(*) FROM audit WHERE actor_id = ? OR target LIKE ?", mum, f"%{mum}%"
        )
        == 0
    )
    acted = db_rows(api.settings, "SELECT actor_id FROM audit WHERE action = 'probe.acted'")
    targeted = db_rows(api.settings, "SELECT target FROM audit WHERE action = 'probe.targeted'")
    token = str(acted[0][0])
    assert re.fullmatch(r"deleted:[0-9a-f]{16}", token)
    assert targeted == [(f"{token}|source-a",)]  # the same token, the tail kept
    removed = db_rows(api.settings, "SELECT target FROM audit WHERE action = 'profile.removed'")
    assert removed == [(token,)]
    # the events that name her are erased in the same transaction, and no `?since=` token issued
    # before can be answered: the clients reload instead of being told of a removal
    assert _count(api, "SELECT COUNT(*) FROM event WHERE actor_id = ?", mum) == 0
    assert _count(api, "SELECT events_purged_through FROM instance") > floor
    # another deletion gets another token: two people never share one
    nan = _seed(api, "nan")
    _sql(
        api,
        "INSERT INTO audit (id, actor_id, action, target) VALUES (?, ?, 'probe.acted', 'x')",
        new_ulid(), nan,
    )  # fmt: skip
    assert _send(api, "DELETE", f"{PROFILES}/{nan}").status_code == 204
    tokens = {
        r[0]
        for r in db_rows(api.settings, "SELECT actor_id FROM audit WHERE action = 'probe.acted'")
    }
    assert len(tokens) == 2


@pytest.mark.req("EF-USR-013", "ENF-OBS-003")
def test_the_deletion_and_the_export_are_audited_without_their_content(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    api.login()
    assert _send(api, "GET", f"{PROFILES}/{mum}/export").status_code == 200
    (line,) = db_rows(
        api.settings, "SELECT actor_id, target, session_id, auth_time FROM audit "
        "WHERE action = 'profile.exported'"
    )  # fmt: skip
    assert line[0] == _id(api, ADMIN) and line[1] == mum and line[2] and line[3]
    assert _send(api, "DELETE", f"{PROFILES}/{mum}").status_code == 204
    # the export line now names her by the token like every other
    (named,) = db_rows(api.settings, "SELECT target FROM audit WHERE action = 'profile.exported'")
    assert str(named[0]).startswith("deleted:")
    dump = json.dumps(db_rows(api.settings, "SELECT action, target FROM audit"))
    assert TITLE not in dump and HIDDEN_TITLE not in dump and "mum" not in dump


# -- the last admin ------------------------------------------------------------------------------


@pytest.mark.req("EF-USR-013", "EF-USR-005")
def test_last_admin_cannot_be_deleted(api: AuthEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    me = _id(api, ADMIN)
    service = _auth(api)
    with pytest.raises(ProfileError) as refused:
        service.profile_remove(me, by=None)
    assert refused.value.code == "last_admin"
    assert run_cli(monkeypatch, api.settings, "profile", "remove", ADMIN) == 1
    assert _count(api, 'SELECT COUNT(*) FROM "user" WHERE id = ?', me) == 1
    # two admins: either may go while the other stays; the survivor is then the last one
    other = service.profile_add("boss", PASSWORD, "admin")
    service.profile_remove(other, by=None)
    with pytest.raises(ProfileError) as again:
        service.profile_remove(me, by=None)
    assert again.value.code == "last_admin"
    assert _count(api, "SELECT COUNT(*) FROM \"user\" WHERE role = 'admin'") == 1


# -- attacks: the export reveals one person, so it is reserved like the deletion -----------------


def _key(api: AuthEnv, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> str:
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "k") == 0
    found = KEY.search(capsys.readouterr().out)
    assert found
    return found.group(0)


@pytest.mark.req("EF-USR-013", "EF-USR-024", "ENF-SEC-010")
def test_export_and_delete_are_refused_to_a_child_a_member_and_every_key(
    api: AuthEnv, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    kid = _seed(api, "kid", "child", max_age_rating=6)
    mum = _seed(api, "mum")
    _auth(api).profile_add("dad", PASSWORD, "member")
    key = _key(api, capsys, monkeypatch)
    api.login()
    admin_id = _id(api, ADMIN)
    before = db_rows(api.settings, 'SELECT id, username, role FROM "user" ORDER BY id')
    targets = (kid, mum, admin_id, "01HZZZZZZZZZZZZZZZZZZZZZZM")  # self, other, admin, unknown
    # a key, whatever its scope: refused before the id is looked at
    for target in targets:
        for method, path in (
            ("GET", f"{PROFILES}/{target}/export"),
            ("DELETE", f"{PROFILES}/{target}"),
        ):
            keyed = api.client.request(method, path, headers={"Authorization": f"Bearer {key}"})
            assert keyed.status_code == 403, path
            assert keyed.json()["type"] == "/api/v1/problems/credential-not-allowed"
    # a child and a member, on themselves and on others: one answer for an id that exists and one
    # that does not (no way to tell a real id from an invented one)
    shapes = set()
    for username, ip in (("kid", "127.0.0.3"), ("mum", "127.0.0.4"), ("dad", "127.0.0.5")):
        client = _as(api, username, ip)
        for target in targets:
            for method, path in (
                ("GET", f"{PROFILES}/{target}/export"),
                ("DELETE", f"{PROFILES}/{target}"),
            ):
                answer = _send(api, method, path, client)
                assert answer.status_code == 403, (username, path)
                shapes.add((answer.status_code, answer.json()["type"]))
    assert len(shapes) == 1
    assert db_rows(api.settings, 'SELECT id, username, role FROM "user" ORDER BY id') == before
    assert _count(api, "SELECT COUNT(*) FROM user_state") == 2
    assert _count(api, "SELECT COUNT(*) FROM audit WHERE action = 'profile.exported'") == 0


@pytest.mark.req("EF-USR-013", "EF-USR-022", "EF-USR-024")
def test_a_stale_admin_session_must_prove_again_to_export_or_delete(api: AuthEnv) -> None:
    mum = _seed(api, "mum")
    api.login()
    api.clock.advance(timedelta(minutes=20))
    for method, path in (("GET", f"{PROFILES}/{mum}/export"), ("DELETE", f"{PROFILES}/{mum}")):
        stale = _send(api, method, path)
        assert stale.status_code == 401, path
        assert stale.json()["type"] == "/api/v1/problems/reauth-required"
    assert _carrying(api, mum)  # nothing went
    assert _count(api, "SELECT COUNT(*) FROM audit WHERE action = 'profile.exported'") == 0


@pytest.mark.req("EF-USR-013", "EF-USR-024")
def test_export_is_a_declared_reserved_action_and_a_route_of_the_openapi(api: AuthEnv) -> None:
    assert RESERVED_ACTIONS["profile.export"].effects
    paths = api.app.openapi()["paths"]
    assert "get" in paths["/api/v1/profiles/{profile_id}/export"]
    assert "delete" in paths["/api/v1/profiles/{profile_id}"]


@pytest.mark.req("EF-USR-013", "ENF-SEC-010")
def test_the_export_of_a_child_holds_no_secret_and_no_title_she_may_not_see(
    api: AuthEnv, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    kid = _seed(api, "kid", "child", max_age_rating=6)
    _sql(api, "UPDATE work SET age_rating = 16 WHERE id = ?", HIDDEN_WORK)
    api.login()
    exported = _send(api, "GET", f"{PROFILES}/{kid}/export")
    assert exported.status_code == 200
    document = exported.json()
    assert document["profile"]["role"] == "child" and document["profile"]["max_age_rating"] == 6
    for text in (HIDDEN_TITLE, TITLE, SECRET_HASH, KEY_HASH, "shk_"):
        assert text not in exported.text
    assert all("hash" not in key for key in document["api_keys"][0])


# -- the host CLI --------------------------------------------------------------------------------


@pytest.mark.req("EF-USR-013", "EF-USR-024")
def test_cli_profile_export_prints_the_json_and_remove_cascades(
    api: AuthEnv, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    mum = _seed(api, "mum")
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "profile", "export", "mum") == 0
    document = json.loads(capsys.readouterr().out)
    assert set(document) == EXPORT_KEYS and document["profile"]["username"] == "mum"
    assert run_cli(monkeypatch, api.settings, "profile", "export", "nobody") == 1
    assert run_cli(monkeypatch, api.settings, "profile", "remove", "mum") == 0
    assert _carrying(api, mum) == []
    assert run_cli(monkeypatch, api.settings, "profile", "export", "mum") == 1
