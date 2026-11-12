"""M3 L19 (EF-USR-014, EF-USR-018): the family search of a child profile, behind the real route.

A child who asks `scope=all` searches only the sources the admin marked "family" (D13) and the
rated catalogue. A remote result has no rating: it is shown reduced (a title, nothing else) with
the one action "request", never "add", never an excerpt, and only if the profile did not choose to
hide the unrated ones (the default does). The subject and word filter drops flagged results before
a page is made, so a dropped result leaves no trace: no count, no total, no cursor of its own.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from tests.auth_env import CSRF_HEADER, PASSWORD, AuthEnv, db_rows, run_cli
from tests.federated_env import CHILD_ID, MEMBER_ID, FakeSource, api_key_of, hit, instance
from tests.visibility_env import HIDDEN_TITLES, VISIBLE_TITLES, add_profile, seed_catalogue

SEARCH = "/api/v1/search"
ALL = {"scope": "all", "q": "tale"}
REDUCED_KEYS = {"title", "action", "request_ref"}
# What must never reach a child from a remote result: more than a title, any way to take it.
FORBIDDEN_WORDS = (
    "cover", "summary", "description", "excerpt", "add_directly", '"add"', "preview", "read",
    "total", "searched_in", "returned", "trust", "editions", "group_id", "format", "official",
    "Alpha texts", "Beta scans", "unavailable", "error",
)  # fmt: skip


def mark_family(api: AuthEnv, key: str, family: bool = True) -> None:
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(
            "INSERT INTO source_config (plugin_id, family_safe) VALUES (?, ?) "
            "ON CONFLICT(plugin_id) DO UPDATE SET family_safe = excluded.family_safe",
            (key, int(family)),
        )
        connection.commit()
    finally:
        connection.close()


def set_profile(api: AuthEnv, profile_id: str, **columns: Any) -> None:
    names = ", ".join(f"{c} = ?" for c in columns)
    values = [json.dumps(v) if isinstance(v, list | dict) else v for v in columns.values()]
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(f'UPDATE "user" SET {names} WHERE id = ?', [*values, profile_id])  # noqa: S608
        connection.commit()
    finally:
        connection.close()


def set_policy(api: AuthEnv, profile_id: str, source_key: str, policy: str) -> None:
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(
            "INSERT INTO profile_source_policy (user_id, source_key, policy, updated_at) "
            "VALUES (?, ?, ?, '2026-11-05 09:00:00')", (profile_id, source_key, policy),
        )  # fmt: skip
        connection.commit()
    finally:
        connection.close()


def kid_client(api: AuthEnv, ip: str = "10.0.0.41") -> TestClient:
    client = api.other_client(ip)
    assert api.login("kid", PASSWORD, client=client).status_code == 200
    return client


def sources() -> tuple[FakeSource, FakeSource]:
    alpha = FakeSource(
        "alpha", name="Alpha texts", kinds=("text",),
        pages=[
            [
                hit(
                    "a1", "A Quiet Tale",
                    memo={"description": "SECRETSUMMARY", "subjects": ["Animals"]},
                ),
                hit("a2", "Erotica Tale", memo={"subjects": ["Erotica"]}),
                hit("a3", "Dragons Tale", memo={"subjects": ["Mythical creatures"]}),
            ],
            [hit("a4", "Second Tale")],
        ],
    )  # fmt: skip
    beta = FakeSource(
        "beta", name="Beta scans", kinds=("images",), pages=[[hit("b1", "Beta Tale")]]
    )
    return alpha, beta


def open_door(api: AuthEnv, *, hide: bool = False) -> None:
    """alpha is a family source; the kid shows the reduced results (`hide=False`)."""
    mark_family(api, "alpha")
    set_profile(api, CHILD_ID, hide_unrated_remote=int(hide))


def titles(body: dict[str, Any]) -> list[str]:
    return [i["title"] for i in body["items"]]


# -- the reduced form ----------------------------------------------------------------------------


@pytest.mark.req("EF-USR-014")
def test_child_search_family_sources_title_only_request_action(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        mark_family(api, "beta", False)
        kid = kid_client(api)
        answer = kid.get(SEARCH, params=ALL)
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert titles(body) == ["A Quiet Tale", "Dragons Tale"]  # the flagged one is dropped
        assert set(body) == {"items", "local", "state", "next_cursor"}  # no total, no counts
        assert body["items"], body
        for item in body["items"]:
            assert set(item) == REDUCED_KEYS  # a title and the one action, nothing else
            assert item["action"] == "request"
        for word in FORBIDDEN_WORDS:
            assert word not in answer.text, word
        assert "SECRETSUMMARY" not in answer.text
        for item in body["items"]:  # the reference says nothing of the source or the result
            assert "alpha" not in item["request_ref"].lower() and item["request_ref"] != "a1"
        assert '"alpha"' not in answer.text  # the summary the source gave stays behind
        assert beta.calls == []  # a source that is not "family" is never asked
        assert "Beta Tale" not in answer.text


@pytest.mark.req("EF-USR-014")
def test_non_family_source_invisible_to_child(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        mark_family(api, "alpha", False)  # nothing is marked: the child has nothing to search
        set_profile(api, CHILD_ID, hide_unrated_remote=0)
        kid = kid_client(api)
        nothing = kid.get(SEARCH, params=ALL)
        assert nothing.status_code == 200 and nothing.json()["items"] == []
        assert alpha.calls == [] and beta.calls == []
        mark_family(api, "alpha")  # now alpha is family ...
        set_policy(api, CHILD_ID, "alpha", "forbidden")  # ... but this child has no door to it
        assert kid.get(SEARCH, params=ALL).json() == nothing.json() and alpha.calls == []
        # the same answer as a source that does not exist at all: nothing says it is there
        assert "alpha" not in nothing.text and "Alpha" not in nothing.text
        # taken off the family list again: closed even if a policy row says "request"
        mark_family(api, "alpha", False)
        assert kid.get(SEARCH, params=ALL).json() == nothing.json() and alpha.calls == []


@pytest.mark.req("EF-USR-014")
def test_a_source_that_is_switched_off_is_not_asked_for_a_child(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta, active=["beta"]) as api:
        open_door(api)
        assert kid_client(api).get(SEARCH, params=ALL).json()["items"] == [] and alpha.calls == []


# -- hide mode -----------------------------------------------------------------------------------


@pytest.mark.req("EF-USR-018")
def test_child_hide_unrated_remote_mode(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        mark_family(api, "alpha")
        kid = kid_client(api)
        # the default of a profile is to hide the unrated ones: the safe side
        assert db_rows(
            api.settings, 'SELECT hide_unrated_remote FROM "user" WHERE id = ?', CHILD_ID
        )[0][0] in (1, True)
        hidden = kid.get(SEARCH, params=ALL)
        assert hidden.status_code == 200
        assert hidden.json() == {"items": [], "local": [], "state": "empty", "next_cursor": None}
        assert alpha.calls == []  # not even asked: nothing was found, nothing is hidden
        absent = kid.get(SEARCH, params={"scope": "all", "q": "zzzzqx"}).json()
        assert absent == hidden.json()  # exactly the answer of a search that found nothing
        set_profile(api, CHILD_ID, hide_unrated_remote=0)
        assert kid.get(SEARCH, params=ALL).json()["items"]
        set_profile(api, CHILD_ID, hide_unrated_remote=1)  # read again at each request
        assert kid.get(SEARCH, params=ALL).json() == hidden.json()


@pytest.mark.req("EF-USR-018")
def test_a_cursor_does_not_outlive_the_hide_mode(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        kid = kid_client(api)
        first = kid.get(SEARCH, params=ALL).json()
        assert first["next_cursor"]
        set_profile(api, CHILD_ID, hide_unrated_remote=1)
        asked = len(alpha.calls)
        later = kid.get(SEARCH, params={**ALL, "cursor": first["next_cursor"]})
        assert later.status_code == 200 and later.json()["items"] == []
        assert len(alpha.calls) == asked
        # and a source taken off the family list stops being walked
        set_profile(api, CHILD_ID, hide_unrated_remote=0)
        mark_family(api, "alpha", False)
        later = kid.get(SEARCH, params={**ALL, "cursor": first["next_cursor"]})
        assert later.status_code in (200, 400) and "Second Tale" not in later.text
        assert len(alpha.calls) == asked


# -- the subject and word filter -----------------------------------------------------------------


@pytest.mark.req("EF-USR-018")
def test_subject_filter_drops_flagged_remote_results(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        kid = kid_client(api)
        first = kid.get(SEARCH, params=ALL)
        assert first.status_code == 200
        assert titles(first.json()) == ["A Quiet Tale", "Dragons Tale"]  # "Erotica" is dropped
        assert "Erotica" not in first.text
        # the profile adds its own subjects and words
        set_profile(api, CHILD_ID, subject_filter=["mythical creatures"])
        assert titles(kid.get(SEARCH, params=ALL).json()) == ["A Quiet Tale"]
        set_profile(api, CHILD_ID, subject_filter=["dragons"])  # a word of the title
        assert titles(kid.get(SEARCH, params=ALL).json()) == ["A Quiet Tale"]
        # a list that is not made of words is ignored, the default list still holds
        set_profile(api, CHILD_ID, subject_filter=[1, None, {"x": 1}])
        assert "Erotica Tale" not in kid.get(SEARCH, params=ALL).text
        # the admin is not filtered: the federated search is hers whole
        assert api.login().status_code == 200
        admin = api.client.get(SEARCH, params=ALL).json()
        assert "Erotica Tale" in str(admin)


@pytest.mark.req("EF-USR-018")
def test_a_dropped_result_leaves_no_trace_in_the_answer(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """A source that only has flagged results answers exactly like one that has none: same body,
    no count, no cursor of its own. A first page of flagged results is read through."""
    flagged = FakeSource(
        "alpha", name="Alpha texts",
        pages=[
            [hit("f1", "Erotica Tale", memo={"subjects": ["Erotica"]})],
            [hit("f2", "Erotica Again", memo={"subjects": ["Erotica"]})],
            [hit("f3", "Third Tale", memo={"subjects": [7]})],  # unreadable subjects: dropped too
        ],
    )  # fmt: skip
    with instance(monkeypatch, settings, flagged) as api:
        open_door(api)
        kid = kid_client(api)
        answer = kid.get(SEARCH, params=ALL).json()
        absent = kid.get(SEARCH, params={"scope": "all", "q": "zzzzqx"}).json()
        assert answer["items"] == [] and answer["next_cursor"] is None
        assert answer["state"] == absent["state"] == "empty"
        assert set(answer) == set(absent)


@pytest.mark.req("EF-USR-018")
def test_a_failing_family_source_is_not_told_to_the_child(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    broken = FakeSource("alpha", name="Alpha texts", error=RuntimeError("http://10.1.2.3/secret"))
    with instance(monkeypatch, settings, broken) as api:
        open_door(api)
        answer = kid_client(api).get(SEARCH, params=ALL)
        assert answer.status_code == 200 and answer.json()["items"] == []
        assert "10.1.2.3" not in answer.text and "error" not in answer.text.lower()
        assert answer.json()["state"] == "empty"


# -- the child's attacks -------------------------------------------------------------------------


@pytest.mark.req("EF-USR-014", "ENF-SEC-010")
def test_a_child_key_never_reaches_the_remote_search(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        key = api_key_of(api, "kid")
        answer = api.other_client("10.0.0.77").get(
            SEARCH, params=ALL, headers={"Authorization": f"Bearer {key}"}
        )
        assert answer.status_code == 403 and "Tale" not in answer.text
        assert alpha.calls == [] and beta.calls == []
        # the local half stays hers by key (L09): the remote half is a session's only
        local = api.other_client("10.0.0.78").get(
            SEARCH, params={"q": "tale"}, headers={"Authorization": f"Bearer {key}"}
        )
        assert local.status_code == 200


@pytest.mark.req("EF-USR-014")
def test_a_child_cannot_use_the_cursor_of_the_admin_and_not_the_other_way_round(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        mark_family(api, "beta", False)
        assert api.login().status_code == 200
        admin_cursor = api.client.get(SEARCH, params=ALL).json()["next_cursor"]
        kid = kid_client(api)
        kid_cursor = kid.get(SEARCH, params=ALL).json()["next_cursor"]
        asked = len(alpha.calls) + len(beta.calls)
        for cursor in (admin_cursor, "x", kid_cursor[:-1], "A" * 5000, ""):
            answer = kid.get(SEARCH, params={**ALL, "cursor": cursor})
            assert answer.status_code in (400, 422), (cursor, answer.text)
            assert "Beta Tale" not in answer.text and "Traceback" not in answer.text
        assert len(alpha.calls) + len(beta.calls) == asked
        stolen = api.client.get(SEARCH, params={**ALL, "cursor": kid_cursor})
        assert stolen.status_code == 400
        other_query = kid.get(SEARCH, params={"scope": "all", "q": "else", "cursor": kid_cursor})
        assert other_query.status_code == 400


@pytest.mark.req("EF-USR-014")
def test_a_demoted_admin_cursor_cannot_open_other_sources_to_the_child(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """An adult walk that holds a non-family source is not a child's walk, even for the same
    person: a walk is made for the form it was started in."""
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        mark_family(api, "beta", False)
        kid = kid_client(api)
        admin_id = db_rows(api.settings, "SELECT id FROM user WHERE role = 'admin'")[0][0]
        assert api.login().status_code == 200
        cursor = api.client.get(SEARCH, params=ALL).json()["next_cursor"]
        set_profile(
            api, admin_id, role="child", max_age_rating=6, hide_unrated_remote=0
        )  # the admin becomes a child
        answer = api.client.get(SEARCH, params={**ALL, "cursor": cursor})
        assert answer.status_code in (400, 403) and "Beta Tale" not in answer.text
        del kid


@pytest.mark.req("EF-USR-014")
def test_member_and_anonymous_still_never_reach_the_remote_search(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        member = api.other_client("10.0.0.42")
        assert api.login("member", PASSWORD, client=member).status_code == 200
        assert member.get(SEARCH, params=ALL).status_code == 403
        assert api.other_client("10.0.0.43").get(SEARCH, params=ALL).status_code in (401, 403)
        assert alpha.calls == [] and beta.calls == []


@pytest.mark.req("EF-USR-014")
def test_a_child_promoted_to_member_loses_the_family_search_at_once(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The scope is read from the profile at each request: a session that began as a child's is
    not one after the role changed, and nothing is asked of a source."""
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        kid = kid_client(api)
        assert kid.get(SEARCH, params=ALL).json()["items"]
        asked = len(alpha.calls)
        set_profile(api, CHILD_ID, role="member", max_age_rating=None)
        answer = kid.get(SEARCH, params=ALL)
        assert answer.status_code in (401, 403) and "Tale" not in answer.text
        assert len(alpha.calls) == asked


@pytest.mark.req("EF-USR-014")
def test_bad_input_of_a_child_is_a_validation_error_never_a_500(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        kid = kid_client(api)
        for params in (
            {"scope": "all"},
            {"scope": "all", "q": "  "},
            {"scope": "everything", "q": "x"},
        ):
            assert kid.get(SEARCH, params=params).status_code in (400, 422), params
        assert alpha.calls == []


@pytest.mark.req("EF-USR-014")
def test_the_catalogue_half_is_the_one_filter_of_l09(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """A child's family search also holds the rated catalogue she may see, through the same filter
    as the local search; a hidden Work is not there."""
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        seed = seed_catalogue(api)
        mark_family(api, "alpha")
        add_profile(api, "kid10", "child", "10.0.0.44", max_age_rating=10)
        assert run_cli(monkeypatch, api.settings, "search", "rebuild") == 0
        client = api.other_client("10.0.0.45")
        assert api.login("kid10", PASSWORD, client=client).status_code == 200
        answer = client.get(SEARCH, params={"scope": "all", "q": "Tale"})
        assert answer.status_code == 200, answer.text
        local = [h["title"] for h in answer.json()["local"]]
        plain = client.get(SEARCH, params={"q": "Tale", "limit": 50}).json()["items"]
        assert sorted(local) == sorted(h["title"] for h in plain) and local
        for title in HIDDEN_TITLES.values():
            assert title not in answer.text
        assert set(local) <= set(VISIBLE_TITLES.values())
        seed.engine.dispose()


# -- the setting of a profile --------------------------------------------------------------------


def _put(api: AuthEnv, client: TestClient, path: str, body: Any) -> Any:
    return client.put(path, json=body, headers={CSRF_HEADER: api.csrf(client)})


@pytest.mark.req("EF-USR-018", "ENF-OBS-003")
def test_the_admin_sets_the_family_search_of_a_profile_and_it_is_audited(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        mark_family(api, "alpha")
        assert api.login().status_code == 200
        path = f"/api/v1/profiles/{CHILD_ID}/family-search"
        done = _put(
            api,
            api.client,
            path,
            {"hide_unrated_remote": False, "subject_filter": ["Dragons", "dragons"]},
        )
        assert done.status_code == 200, done.text
        assert done.json() == {"hide_unrated_remote": False, "subject_filter": ["dragons"]}
        assert kid_client(api).get(SEARCH, params=ALL).json()["items"]
        listed = api.client.get("/api/v1/profiles").json()["items"]
        kid = next(p for p in listed if p["id"] == CHILD_ID)
        assert kid["hide_unrated_remote"] is False and kid["subject_filter"] == ["dragons"]
        rows = db_rows(
            api.settings, "SELECT target FROM audit WHERE action = ?", "profile.family_search_set"
        )
        assert len(rows) == 1 and str(rows[0][0]).startswith(CHILD_ID)
        assert "dragons" not in str(rows[0][0])  # who and what changed, never the words
        _put(api, api.client, path, {"hide_unrated_remote": False, "subject_filter": ["dragons"]})
        assert (
            len(
                db_rows(
                    api.settings,
                    "SELECT 1 FROM audit WHERE action = ?",
                    "profile.family_search_set",
                )
            )
            == 1
        )
        for bad in (
            {"hide_unrated_remote": "no", "subject_filter": []},
            {"hide_unrated_remote": True, "subject_filter": "dragons"},
            {"hide_unrated_remote": True, "subject_filter": [1]},
            {"hide_unrated_remote": True, "subject_filter": ["x"] * 101},
            {"hide_unrated_remote": True, "subject_filter": ["x" * 65]},
            {"hide_unrated_remote": True, "subject_filter": [], "extra": 1},
            {"hide_unrated_remote": True, "subject_filter": ["--"]},  # folds to nothing: refused
            {"hide_unrated_remote": True, "subject_filter": ["ok", "   "]},
            {"subject_filter": []},
        ):
            assert _put(api, api.client, path, bad).status_code == 422, bad
        assert (
            _put(
                api,
                api.client,
                "/api/v1/profiles/01J000000000000000000NONE0/family-search",
                {"hide_unrated_remote": True, "subject_filter": []},
            ).status_code
            == 404
        )


@pytest.mark.req("EF-USR-018", "ENF-SEC-010")
def test_a_child_a_member_or_a_key_cannot_change_the_family_search(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        mark_family(api, "alpha")
        path = f"/api/v1/profiles/{CHILD_ID}/family-search"
        loosen = {"hide_unrated_remote": False, "subject_filter": []}
        kid = kid_client(api)
        member = api.other_client("10.0.0.46")
        assert api.login("member", PASSWORD, client=member).status_code == 200
        for client in (kid, member):
            assert _put(api, client, path, loosen).status_code == 403
        admin_key = api_key_of(api, "admin", read_only=False)
        keyed = api.other_client("10.0.0.47").put(
            path, json=loosen, headers={"Authorization": f"Bearer {admin_key}"}
        )
        assert keyed.status_code in (401, 403)
        stored = db_rows(
            api.settings, 'SELECT hide_unrated_remote FROM "user" WHERE id = ?', CHILD_ID
        )
        assert stored[0][0] in (1, True)  # still hidden
        assert kid.get(SEARCH, params=ALL).json()["items"] == []


@pytest.mark.req("EF-USR-014")
def test_no_source_loaded_is_the_empty_answer_for_a_child(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from tests.auth_env import auth_env

    with auth_env(monkeypatch, settings) as api:
        add_profile(api, "kid", "child", "10.0.0.2", max_age_rating=6)
        kid_id = db_rows(api.settings, "SELECT id FROM user WHERE username = 'kid'")[0][0]
        set_profile(api, kid_id, hide_unrated_remote=0)
        kid = kid_client(api)
        answer = kid.get(SEARCH, params=ALL)
        assert answer.status_code == 200
        assert answer.json() == {"items": [], "local": [], "state": "empty", "next_cursor": None}


@pytest.mark.req("EF-USR-014", "EF-USR-018")
def test_the_family_settings_are_read_from_the_row_and_closed_when_it_is_not_there(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from shelfhold.identity.public import family_profile, family_source_keys
    from shelfhold.infrastructure.db import create_db_engine

    alpha, _ = sources()
    with instance(monkeypatch, settings, alpha) as api:
        mark_family(api, "alpha")
        set_profile(api, CHILD_ID, hide_unrated_remote=0, subject_filter=["dragons"])
        engine = create_db_engine(api.settings.database_path)
        try:
            known = family_profile(engine, CHILD_ID)
            assert known.scope.kind == "ceiling" and known.hide_unrated_remote is False
            assert known.subject_filter == ["dragons"]
            for nobody in (None, "", "01J000000000000000000NONE0"):
                gone = family_profile(engine, nobody)
                assert gone.scope.kind == "none" and gone.hide_unrated_remote is True
            with engine.connect() as connection:
                assert family_source_keys(connection, CHILD_ID, "child", []) == frozenset()
                assert family_source_keys(connection, CHILD_ID, "child", ["alpha", "x"]) == {
                    "alpha"
                }
                # a policy row of another profile closes nothing for this one
                set_policy(api, MEMBER_ID, "alpha", "forbidden")
            with engine.connect() as connection:
                assert family_source_keys(connection, CHILD_ID, "child", ["alpha"]) == {"alpha"}
                assert family_source_keys(connection, MEMBER_ID, "child", ["alpha"]) == frozenset()
        finally:
            engine.dispose()


@pytest.mark.req("EF-USR-018")
def test_a_word_in_another_script_is_kept_and_filters(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        mark_family(api, "alpha")
        assert api.login().status_code == 200
        path = f"/api/v1/profiles/{CHILD_ID}/family-search"
        word = "дракон"  # a Cyrillic word
        done = _put(api, api.client, path, {"hide_unrated_remote": False, "subject_filter": [word]})
        assert done.status_code == 200 and done.json()["subject_filter"] == [word]


@pytest.mark.req("EF-USR-018", "ENF-SEC-010")
def test_the_family_search_of_a_member_is_refused_and_a_demotion_resets_it(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        assert api.login().status_code == 200
        loosen = {"hide_unrated_remote": False, "subject_filter": ["x"]}
        refused = _put(api, api.client, f"/api/v1/profiles/{MEMBER_ID}/family-search", loosen)
        assert refused.status_code in (400, 409, 422), refused.text
        set_profile(api, CHILD_ID, hide_unrated_remote=0, subject_filter=["x"])
        patched = api.client.patch(
            f"/api/v1/profiles/{CHILD_ID}",
            json={"role": "member", "max_age_rating": None, "can_rate": False},
            headers={CSRF_HEADER: api.csrf()},
        )
        assert patched.status_code == 200, patched.text
        rows = db_rows(
            api.settings,
            'SELECT hide_unrated_remote, subject_filter FROM "user" WHERE id = ?',
            CHILD_ID,
        )
        assert rows[0][0] in (1, True) and json.loads(rows[0][1]) == []


@pytest.mark.req("EF-USR-014")
def test_a_source_that_says_it_is_not_for_children_is_never_asked(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    alpha.manifest = alpha.manifest.model_copy(update={"content_warning": "mixed"})
    with instance(monkeypatch, settings, alpha, beta) as api:
        open_door(api)
        assert kid_client(api).get(SEARCH, params=ALL).json()["items"] == []
        assert alpha.calls == []
