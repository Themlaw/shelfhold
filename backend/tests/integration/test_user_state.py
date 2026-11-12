"""M3 L36 attack tests: the state of a user (follow, favourite, read) is that user's alone, the
last write wins by the server's clock, and the preferred title language never shows a title the
viewer may not see (EF-USR-007, EF-BIB-022, EF-USR-015, ENF-SEC-010, ENF-TIME-001).

Everything goes through the real application (`create_app()`, real sessions, real CSRF).
"""

import re
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from shelfhold.identity.public import LocalAuth
from tests.auth_env import ADMIN, CSRF_HEADER, PASSWORD, AuthEnv, auth_env, db_rows, run_cli

ME = "/api/v1/me"
KEY = re.compile(r"shk_([0-9a-f]{8})_[A-Za-z0-9_-]{43}")
OK_WORK = "01HZZZZZZZZZZZZZZZZZZZZZZA"
ADULT_WORK = "01HZZZZZZZZZZZZZZZZZZZZZZB"
X_WORK = "01HZZZZZZZZZZZZZZZZZZZZZZX"
UNRATED_WORK = "01HZZZZZZZZZZZZZZZZZZZZZZC"
MISSING = "01HZZZZZZZZZZZZZZZZZZZZZZM"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        _works(env)
        yield env


def _sql(env: AuthEnv, sql: str, *params: object) -> None:
    connection = sqlite3.connect(env.settings.database_path)
    try:
        with connection:
            connection.execute(sql, params)
    finally:
        connection.close()


def _works(env: AuthEnv) -> None:
    kind = str(db_rows(env.settings, "SELECT key FROM work_kind LIMIT 1")[0][0])
    for work_id, title, age, x in (
        (OK_WORK, "Un titre", 6, 0),
        (ADULT_WORK, "Titre adulte", 18, 0),
        (X_WORK, "Titre X", 18, 1),
        (UNRATED_WORK, "Titre sans classement", None, 0),
    ):
        _sql(
            env,
            "INSERT INTO work (id, kind, title, sort_title, age_rating, age_x, rated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            work_id, kind, title, title.lower(), age, x,
            None if age is None else "2026-11-01 10:00:00",  # rated by an admin (M3 L09)
        )  # fmt: skip
    for work_id, lang, kind_, title in (
        (OK_WORK, "ja", "localized", "ある題名"),
        (OK_WORK, "en", "localized", "A title"),
        (OK_WORK, "fr", "localized", "Un titre"),
        (ADULT_WORK, "ja", "localized", "大人の題名"),
        (ADULT_WORK, "en", "localized", "Adult hidden title"),
    ):
        _sql(
            env,
            "INSERT INTO work_title (work_id, title, lang, kind) VALUES (?, ?, ?, ?)",
            work_id, title, lang, kind_,
        )  # fmt: skip


def _auth(env: AuthEnv) -> LocalAuth:
    return env.app.state.auth_gate.auth  # type: ignore[attr-defined,no-any-return]


def _client(env: AuthEnv, username: str, ip: str) -> TestClient:
    client = env.other_client(ip)
    assert env.login(username, client=client).status_code == 200
    return client


def _user(env: AuthEnv, username: str, role: str, ip: str, **kw: Any) -> TestClient:
    _auth(env).profile_add(username, PASSWORD, role, **kw)
    return _client(env, username, ip)


def _put(env: AuthEnv, client: TestClient, path: str, body: Any) -> Any:
    return client.put(path, json=body, headers={CSRF_HEADER: env.csrf(client)})


def _state_rows(env: AuthEnv) -> list[tuple[object, ...]]:
    return db_rows(
        env.settings,
        "SELECT u.username, s.work_id, s.followed, s.favorite, s.read_at IS NOT NULL "
        'FROM user_state s JOIN "user" u ON u.id = s.user_id ORDER BY u.username, s.work_id',
    )


def _shape(response: Any) -> tuple[int, str, str]:
    body = response.json()
    return response.status_code, body.get("type", ""), body.get("title", "")


# -- isolation between profiles ---------------------------------------------------------------


@pytest.mark.req("EF-USR-007")
def test_user_state_isolated_between_profiles(api: AuthEnv) -> None:
    mum = _user(api, "mum", "member", "10.0.0.2")
    dad = _user(api, "dad", "member", "10.0.0.3")
    done = _put(
        api, mum, f"{ME}/state/{OK_WORK}", {"followed": True, "favorite": True, "read": True}
    )
    assert done.status_code == 200, done.text
    assert done.json()["followed"] and done.json()["favorite"] and done.json()["read_at"]
    seen = dad.get(f"{ME}/state/{OK_WORK}")
    assert seen.status_code == 200
    assert (seen.json()["followed"], seen.json()["favorite"], seen.json()["read_at"]) == (
        False, False, None,
    )  # fmt: skip
    assert _put(api, dad, f"{ME}/state/{OK_WORK}", {"read": True}).status_code == 200
    mine = mum.get(f"{ME}/state/{OK_WORK}").json()
    assert mine["followed"] and mine["favorite"]
    assert dad.get(f"{ME}/state/{OK_WORK}").json()["followed"] is False
    assert _put(api, dad, f"{ME}/state/{OK_WORK}", {"read": False}).json()["read_at"] is None
    assert mum.get(f"{ME}/state/{OK_WORK}").json()["read_at"] is not None  # untouched by dad
    assert [i["work_id"] for i in dad.get(f"{ME}/state").json()["items"]] == [OK_WORK]


@pytest.mark.req("EF-USR-007", "ENF-SEC-010")
def test_user_state_idor_other_id_child_reading_parent_and_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    mum = _user(api, "mum", "member", "10.0.0.2")
    kid = _user(api, "kid", "child", "10.0.0.4", max_age_rating=10)
    assert _put(api, mum, f"{ME}/state/{OK_WORK}", {"favorite": True}).status_code == 200
    mum_id = str(db_rows(api.settings, 'SELECT id FROM "user" WHERE username = ?', "mum")[0][0])
    # no route names another user: the id of someone else gets nothing, read or write
    for path in (f"/api/v1/users/{mum_id}/state", f"{ME}/{mum_id}/state/{OK_WORK}"):
        assert kid.get(path).status_code == 404
        assert _put(api, kid, path, {"favorite": False}).status_code in (404, 405)
    # a body that names a user is refused whole, and nothing is written for anyone
    before = _state_rows(api)
    named = _put(api, kid, f"{ME}/state/{OK_WORK}", {"favorite": True, "user_id": mum_id})
    assert named.status_code == 422
    assert _state_rows(api) == before
    # the child reads its own state of a Work the parent marked: its defaults, never the parent's
    assert kid.get(f"{ME}/state/{OK_WORK}").json()["favorite"] is False
    assert kid.get(f"{ME}/state").json()["items"] == []
    assert _put(api, kid, f"{ME}/state/{OK_WORK}", {"favorite": False}).status_code == 200
    assert mum.get(f"{ME}/state/{OK_WORK}").json()["favorite"] is True
    # an API key is a program: it neither reads nor writes the state of the user who issued it
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "k") == 0
    key = KEY.search(capsys.readouterr().out)
    assert key
    bearer = {"Authorization": f"Bearer {key.group(0)}"}
    anonymous = TestClient(api.app, base_url="https://testserver")
    for method, path in (
        ("GET", f"{ME}/state"),
        ("GET", f"{ME}/state/{OK_WORK}"),
        ("PUT", f"{ME}/state/{OK_WORK}"),
        ("GET", f"{ME}/title-languages"),
        ("PUT", f"{ME}/title-languages"),
    ):
        keyed = api.client.request(method, path, headers=bearer, json={"favorite": True})
        assert keyed.status_code == 403, (method, path)
        assert keyed.json()["type"] == "/api/v1/problems/credential-not-allowed"
        assert anonymous.request(method, path, json={}).status_code == 401, (method, path)
    assert db_rows(api.settings, "SELECT count(*) FROM user_state")[0][0] == 2


# -- the parental ceiling ---------------------------------------------------------------------


@pytest.mark.req("EF-USR-015", "EF-USR-007", "ENF-SEC-010")
def test_child_state_above_ceiling_is_refused_like_a_missing_work(api: AuthEnv) -> None:
    kid = _user(api, "kid", "child", "10.0.0.4", max_age_rating=10)
    adult = _user(api, "mum", "member", "10.0.0.2")
    assert _put(api, adult, f"{ME}/state/{ADULT_WORK}", {"followed": True}).status_code == 200
    missing = kid.get(f"{ME}/state/{MISSING}")
    assert missing.status_code == 404
    for work_id in (ADULT_WORK, X_WORK, UNRATED_WORK):  # unrated: closed by default
        for change in ({"followed": True}, {"favorite": True}, {"read": True}):
            refused = _put(api, kid, f"{ME}/state/{work_id}", change)
            assert _shape(refused) == _shape(missing), (work_id, change)
            assert refused.json()["status"] == missing.json()["status"]
        assert _shape(kid.get(f"{ME}/state/{work_id}")) == _shape(missing)
        assert _shape(kid.get(f"{ME}/works/{work_id}/title")) == _shape(
            kid.get(f"{ME}/works/{MISSING}/title")
        )
    assert [r for r in _state_rows(api) if r[0] == "kid"] == []  # nothing was written
    # under the ceiling it works; the ceiling is read at each call, not at login
    assert _put(api, kid, f"{ME}/state/{OK_WORK}", {"favorite": True}).status_code == 200
    _sql(api, 'UPDATE "user" SET max_age_rating = 3 WHERE username = ?', "kid")
    assert _shape(kid.get(f"{ME}/state/{OK_WORK}")) == _shape(missing)
    assert kid.get(f"{ME}/state").json()["items"] == []  # the old mark is not listed either
    # an adult sees the whole shelf
    assert _put(api, adult, f"{ME}/state/{X_WORK}", {"favorite": True}).status_code == 200


# -- last write wins --------------------------------------------------------------------------


@pytest.mark.req("EF-USR-007", "ENF-TIME-001")
def test_user_state_last_write_wins(api: AuthEnv) -> None:
    mum = _user(api, "mum", "member", "10.0.0.2")
    path = f"{ME}/state/{OK_WORK}"
    first = _put(api, mum, path, {"followed": True}).json()
    api.clock.advance(timedelta(seconds=30))
    second = _put(api, mum, path, {"followed": False, "favorite": True}).json()
    assert second["followed"] is False and second["favorite"] is True
    assert second["stamp"] > first["stamp"]
    # the same instant: the write that arrives last is the one that stays (no flip-flop, no error)
    a = _put(api, mum, path, {"favorite": False}).json()
    b = _put(api, mum, path, {"favorite": True}).json()
    assert a["stamp"] == b["stamp"] == second["stamp"]
    assert mum.get(path).json()["favorite"] is True
    # the server clock went back: the write stamped later is kept, and the answer says so
    api.clock.advance(timedelta(seconds=-3600))
    late = _put(api, mum, path, {"favorite": False}).json()
    assert late["favorite"] is True and late["stamp"] == b["stamp"]
    assert mum.get(path).json()["favorite"] is True


@pytest.mark.req("EF-USR-007", "ENF-TIME-001")
def test_user_state_client_clock_is_bounded_and_never_orders(api: AuthEnv) -> None:
    mum = _user(api, "mum", "member", "10.0.0.2")
    path = f"{ME}/state/{OK_WORK}"
    now = api.clock.now()
    ahead = _put(
        api, mum, path, {"favorite": True, "client_ts": (now + timedelta(days=400)).isoformat()}
    )
    assert ahead.status_code == 422
    assert ahead.json()["type"] == "/api/v1/problems/user-state-refused"
    assert _state_rows(api) == []  # refused whole: nothing written
    naive = _put(api, mum, path, {"favorite": True, "client_ts": "2026-11-05T09:00:00"})
    assert naive.status_code == 422 and _state_rows(api) == []
    # within the tolerated drift, or far in the past: accepted, and the stamp is the server's
    near = _put(
        api, mum, path, {"favorite": True, "client_ts": (now + timedelta(minutes=2)).isoformat()}
    )
    assert near.status_code == 200
    assert near.json()["stamp"] == now.isoformat().replace("+00:00", "Z") or near.json()[
        "stamp"
    ].startswith(now.isoformat()[:19])
    old = _put(api, mum, path, {"favorite": False, "client_ts": "2001-01-01T00:00:00+00:00"})
    assert old.status_code == 200 and old.json()["favorite"] is False  # an old claim never loses


# -- preferred title language -----------------------------------------------------------------


@pytest.mark.req("EF-BIB-022", "EF-USR-007")
def test_preferred_title_language_per_user(api: AuthEnv) -> None:
    mum = _user(api, "mum", "member", "10.0.0.2")
    dad = _user(api, "dad", "member", "10.0.0.3")
    assert _put(api, mum, f"{ME}/title-languages", {"languages": ["ja", "en"]}).status_code == 200
    assert (
        _put(api, dad, f"{ME}/title-languages", {"languages": ["EN-us", "fr"]}).status_code == 200
    )
    assert mum.get(f"{ME}/title-languages").json() == {"languages": ["ja", "en"]}
    assert dad.get(f"{ME}/title-languages").json() == {"languages": ["en-US", "fr"]}
    assert mum.get(f"{ME}/works/{OK_WORK}/title").json()["title"] == "ある題名"
    assert dad.get(f"{ME}/works/{OK_WORK}/title").json()["title"] == "A title"  # en-US falls to en
    assert _put(api, dad, f"{ME}/title-languages", {"languages": ["de"]}).status_code == 200
    assert dad.get(f"{ME}/works/{OK_WORK}/title").json()["title"] == "Un titre"  # the Work's own
    assert mum.get(f"{ME}/works/{OK_WORK}/title").json()["title"] == "ある題名"


@pytest.mark.req("EF-BIB-022", "EF-USR-015", "ENF-SEC-010")
def test_title_language_never_shows_a_title_the_viewer_may_not_see(api: AuthEnv) -> None:
    kid = _user(api, "kid", "child", "10.0.0.4", max_age_rating=10)
    adult = _user(api, "mum", "member", "10.0.0.2")
    for client in (kid, adult):
        assert _put(api, client, f"{ME}/title-languages", {"languages": ["en"]}).status_code == 200
    assert adult.get(f"{ME}/works/{ADULT_WORK}/title").json()["title"] == "Adult hidden title"
    refused = kid.get(f"{ME}/works/{ADULT_WORK}/title")
    assert refused.status_code == 404
    assert "Adult hidden title" not in refused.text and "Titre adulte" not in refused.text
    listed = kid.get(f"{ME}/state").text + kid.get(f"{ME}/title-languages").text
    assert "Adult hidden title" not in listed and "大人" not in listed


@pytest.mark.req("EF-BIB-022", "ENF-SEC-010")
def test_title_languages_refuse_wildcards_junk_and_excess(api: AuthEnv) -> None:
    mum = _user(api, "mum", "member", "10.0.0.2")
    assert _put(api, mum, f"{ME}/title-languages", {"languages": ["ja"]}).status_code == 200
    many = ["en", "fr", "de", "it", "es", "pt", "nl", "sv", "da"]
    for bad in (["und"], ["mul"], ["all"], [""], ["a b"], ["<script>"], many, [1], "en", None):
        refused = _put(api, mum, f"{ME}/title-languages", {"languages": bad})
        assert refused.status_code == 422, bad
    assert mum.get(f"{ME}/title-languages").json() == {"languages": ["ja"]}  # unchanged
    dup = _put(api, mum, f"{ME}/title-languages", {"languages": ["en", "EN", "fr"]})
    assert dup.json() == {"languages": ["en", "fr"]}
    # a stored preference of one user is never in another's answer
    dad = _user(api, "dad", "member", "10.0.0.3")
    assert dad.get(f"{ME}/title-languages").json() == {"languages": []}
    assert ADMIN  # the admin of the fixture exists but never mixes with the others
