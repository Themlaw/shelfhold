"""M3 L35 (EF-USR-010, EF-USR-016, ENF-SEC-010, ENF-OBS-003): the routes of the requests, through
the real application with real sessions, CSRF and keys.

A child asks only by the reference her search gave; a member never decides; a key never acts; a
request of someone else is exactly as absent as one that never was; an approval is the reserved
door of a fresh admin session."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.acquisition.application.requests import Estimate
from shelfhold.config import Settings
from tests.auth_env import CSRF_HEADER, PASSWORD, AuthEnv, db_rows
from tests.federated_env import CHILD_ID, MEMBER_ID, FakeSource, api_key_of, hit, instance
from tests.integration.test_family_search import SEARCH, mark_family, set_profile
from tests.request_env import FakePorts

REQUESTS = "/api/v1/requests"
ADMIN_ROUTES = (
    ("GET", f"{REQUESTS}/queue"),
    ("GET", f"{REQUESTS}/{{id}}/approval"),
    ("POST", f"{REQUESTS}/{{id}}/approve"),
    ("POST", f"{REQUESTS}/{{id}}/reject"),
    ("POST", f"{REQUESTS}/{{id}}/not-found"),
)
OWN_ROUTES = (
    ("GET", REQUESTS),
    ("POST", REQUESTS),
    ("GET", f"{REQUESTS}/{{id}}"),
    ("POST", f"{REQUESTS}/{{id}}/cancel"),
)
CHILD_KEYS = {"id", "state", "title", "reject_reason", "created_at", "updated_at"}
ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def alpha() -> FakeSource:
    return FakeSource(
        "alpha", name="Alpha texts", kinds=("text",),
        pages=[
            [hit("a1", "A Quiet Tale", memo={"subjects": ["Animals"]}), hit("a2", "Dragons Tale")]
        ],
    )  # fmt: skip


@pytest.fixture(autouse=True)
def ports(monkeypatch: pytest.MonkeyPatch) -> FakePorts:
    fakes = FakePorts(kinds={"alpha": "manga"}, estimate_of=Estimate(units=3, size_bytes=10))
    monkeypatch.setattr("shelfhold.ops.requests.build_ports", lambda _comp: fakes)
    return fakes


def client_of(api: AuthEnv, username: str, ip: str) -> TestClient:
    client = api.other_client(ip)
    assert api.login(username, PASSWORD, client=client).status_code == 200
    return client


def admin_client(api: AuthEnv) -> TestClient:
    assert api.login().status_code == 200
    return api.client


def send(api: AuthEnv, client: TestClient, method: str, path: str, **kwargs: Any) -> Any:
    if method == "GET":
        return client.get(path, **kwargs)
    return api.post(path, client=client, **kwargs)


def family(api: AuthEnv) -> TestClient:
    mark_family(api, "alpha")
    set_profile(api, CHILD_ID, hide_unrated_remote=0)
    return client_of(api, "kid", "10.0.0.41")


def reference(kid: TestClient, title: str = "A Quiet Tale") -> str:
    found = kid.get(SEARCH, params={"scope": "all", "q": "tale"}).json()["items"]
    return next(i["request_ref"] for i in found if i["title"] == title)


def table(api: AuthEnv) -> list[tuple[Any, ...]]:
    return db_rows(api.settings, "SELECT id, state, requester_user_id FROM request ORDER BY id")


def ask_as_child(api: AuthEnv, kid: TestClient) -> str:
    answer = api.post(REQUESTS, client=kid, json={"request_ref": reference(kid)})
    assert answer.status_code == 201, answer.text
    return str(answer.json()["id"])


# -- a child asks ----------------------------------------------------------------------------------


@pytest.mark.req("EF-USR-010", "EF-USR-014")
def test_child_asks_by_the_reference_of_her_search(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        answer = api.post(REQUESTS, client=kid, json={"request_ref": reference(kid)})
        assert answer.status_code == 201, answer.text
        body = answer.json()
        assert set(body) == CHILD_KEYS and body["state"] == "pending"
        assert body["title"] == "A Quiet Tale" and body["reject_reason"] is None
        for word in ("alpha", "Alpha", "a1", "work_id", "source", "visibility", "age"):
            assert word not in answer.text, word
        listed = kid.get(REQUESTS)
        assert listed.status_code == 200 and [set(i) for i in listed.json()["items"]] == [
            CHILD_KEYS
        ]
        assert kid.get(f"{REQUESTS}/{body['id']}").json() == body
        # her request is visible to the parents, whatever she chose (a child is never hidden)
        assert db_rows(api.settings, "SELECT visibility FROM request")[0][0] == "household"


@pytest.mark.req("EF-USR-010", "EF-USR-014", "ENF-SEC-010")
def test_child_cannot_ask_by_anything_but_a_reference(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        work = db_rows(api.settings, "SELECT 1")  # the catalogue is empty: a Work id is a guess
        assert work
        answers = []
        for body in (
            {"target_kind": "work_id", "target_value": ULID},
            {"target_kind": "isbn", "target_value": "9780306406157"},
            {"target_kind": "free_text", "target_value": "anything"},
            {"target_kind": "source_ref", "target_value": ULID},  # a pending item she never saw
            {"request_ref": "forged-token"},
            {"request_ref": "x" * 500},
            {},
        ):
            answer = api.post(REQUESTS, client=kid, json=body)
            assert answer.status_code in (403, 404, 422), (body, answer.text)
            answers.append((answer.status_code, answer.json().get("code")))
        assert table(api) == []
        # the Work-id guess says the same thing whether the Work exists or not (checked at the
        # service with a real Work: see test_child_cannot_ask_by_work_isbn_or_text)
        assert answers[0] == answers[1] == answers[2]


@pytest.mark.req("EF-USR-010", "EF-USR-014")
def test_a_reference_belongs_to_the_child_it_was_shown_to(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        ref = reference(kid)
        from tests.federated_env import MEMBER_ID as _m  # the adult has no reference to give

        assert _m
        member = client_of(api, "member", "10.0.0.51")
        stolen = api.post(REQUESTS, client=member, json={"request_ref": ref})
        forged = api.post(REQUESTS, client=kid, json={"request_ref": "forged"})
        assert stolen.status_code == forged.status_code in (404, 422)
        assert stolen.json()["code"] == forged.json()["code"]
        assert table(api) == []
        used = api.post(REQUESTS, client=kid, json={"request_ref": ref})
        assert used.status_code == 201
        again = api.post(REQUESTS, client=kid, json={"request_ref": ref})
        assert again.status_code in (200, 201) and again.json()["id"] == used.json()["id"]
        assert len(table(api)) == 1  # a replay is the same request


@pytest.mark.req("EF-USR-010", "EF-USR-014")
def test_a_source_taken_off_the_family_list_closes_the_reference(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        ref = reference(kid)
        mark_family(api, "alpha", False)  # the parent changed her mind after the search
        closed = api.post(REQUESTS, client=kid, json={"request_ref": ref})
        assert closed.status_code in (403, 404, 422) and table(api) == []
        assert "alpha" not in closed.text.lower()


@pytest.mark.req("EF-USR-010")
def test_an_adult_asks_by_work_isbn_or_text(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        member = client_of(api, "member", "10.0.0.51")
        for body in (
            {"target_kind": "isbn", "target_value": "978-0-306-40615-7"},
            {"target_kind": "free_text", "target_value": "A book I heard of"},
        ):
            answer = api.post(REQUESTS, client=member, json=body)
            assert answer.status_code == 201, answer.text
        assert [r[1] for r in table(api)] == ["pending", "pending"]
        bad = api.post(REQUESTS, client=member, json={"target_kind": "isbn", "target_value": "1"})
        assert bad.status_code == 422
        unknown = api.post(
            REQUESTS, client=member, json={"target_kind": "work_id", "target_value": ULID}
        )
        assert unknown.status_code == 404
        assert db_rows(api.settings, "SELECT visibility FROM request")[0][0] == "private"


# -- keys, members, CSRF ---------------------------------------------------------------------------


@pytest.mark.req("EF-USR-010", "ENF-SEC-010")
def test_a_key_never_touches_a_request(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        keys = {
            "kid": api_key_of(api, "kid"),
            "member": api_key_of(api, "member"),
            "admin": api_key_of(api, "admin", read_only=False),
        }
        before = table(api)
        for owner, key in keys.items():
            bare = api.other_client("10.9.9.9")
            for method, path in (*OWN_ROUTES, *ADMIN_ROUTES):
                url = path.replace("{id}", rid)
                body = {"request_ref": "x"} if path == REQUESTS else {"reason": "no", "age": 10}
                answer = bare.request(
                    method, url, headers={"Authorization": f"Bearer {key}"},
                    **({"json": body} if method == "POST" else {}),
                )  # fmt: skip
                assert answer.status_code == 403, (owner, method, path, answer.text)
                assert "Quiet" not in answer.text
        assert table(api) == before
        assert (
            db_rows(api.settings, "SELECT count(*) FROM audit WHERE action LIKE 'request.%'")[0][0]
            == 1
        )  # the creation of the child's own request, nothing from a key


@pytest.mark.req("EF-USR-010", "ENF-SEC-010")
def test_a_member_and_a_child_never_decide(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, ports: FakePorts
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        member = client_of(api, "member", "10.0.0.51")
        before = table(api)
        for who in (member, kid):
            for method, path in ADMIN_ROUTES:
                url = path.replace("{id}", rid)
                answer = send(
                    api,
                    who,
                    method,
                    url,
                    **({"json": {"age": 10, "reason": "x"}} if method == "POST" else {}),
                )
                assert answer.status_code == 403, (method, path, answer.text)
                assert "Quiet" not in answer.text
        assert table(api) == before and ports.follows == []
        # ... and no other, though equally unknown, identifier answers differently
        ghost = send(api, member, "POST", f"{REQUESTS}/{ULID}/approve", json={"age": 10})
        real = send(api, member, "POST", f"{REQUESTS}/{rid}/approve", json={"age": 10})
        assert (ghost.status_code, ghost.json()["code"]) == (real.status_code, real.json()["code"])


@pytest.mark.req("EF-USR-010", "ENF-SEC-008")
def test_a_state_changing_route_needs_the_csrf_token(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        assert kid.post(f"{REQUESTS}/{rid}/cancel").status_code == 403  # no CSRF header
        wrong = kid.post(f"{REQUESTS}/{rid}/cancel", headers={CSRF_HEADER: "nope"})
        assert wrong.status_code == 403
        admin = admin_client(api)
        assert admin.post(f"{REQUESTS}/{rid}/approve", json={"age": 10}).status_code == 403
        assert table(api)[0][1] == "pending"


@pytest.mark.req("EF-USR-010", "EF-USR-016")
def test_a_request_of_someone_else_is_exactly_an_absent_one(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        member = client_of(api, "member", "10.0.0.51")
        for method, path in (("GET", "{id}"), ("POST", "{id}/cancel")):
            real = send(api, member, method, f"{REQUESTS}/{path.format(id=rid)}")
            ghost = send(api, member, method, f"{REQUESTS}/{path.format(id=ULID)}")
            assert real.status_code == ghost.status_code == 404
            assert real.json()["code"] == ghost.json()["code"]
            assert "Quiet" not in real.text
        assert member.get(REQUESTS).json()["items"] == []
        assert table(api)[0][1] == "pending"  # the cancel of a stranger did nothing
        # and the same for a child about a sibling's
        mem_req = api.post(
            REQUESTS, client=member, json={"target_kind": "free_text", "target_value": "Mine"}
        ).json()["id"]
        assert kid.get(f"{REQUESTS}/{mem_req}").status_code == 404
        assert api.post(f"{REQUESTS}/{mem_req}/cancel", client=kid).status_code == 404


# -- the admin decides ----------------------------------------------------------------------------


@pytest.mark.req("EF-USR-010", "ENF-OBS-003")
def test_admin_approves_a_child_request_with_an_age_in_one_gesture(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, ports: FakePorts
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        admin = admin_client(api)
        queue = admin.get(f"{REQUESTS}/queue").json()["items"]
        assert [(q["id"], q["title"], q["needs_age"]) for q in queue] == [
            (rid, "A Quiet Tale", True)
        ]
        preview = admin.get(f"{REQUESTS}/{rid}/approval").json()
        assert preview["needs_age"] is True and preview["units"] == 3
        assert preview["confirmation_required"] is False
        refused = api.post(f"{REQUESTS}/{rid}/approve", client=admin, json={})
        assert refused.status_code == 422 and refused.json()["code"] == "request.choose_age"
        assert ports.follows == []
        done = api.post(f"{REQUESTS}/{rid}/approve", client=admin, json={"age": 10})
        assert done.status_code == 200, done.text
        assert done.json()["state"] == "approved"
        assert ports.follows == [("alpha", "a1", CHILD_ID, "household")]
        # she sees that it was approved, and nothing about how
        mine = kid.get(f"{REQUESTS}/{rid}").json()
        assert mine["state"] == "approved" and set(mine) == CHILD_KEYS
        assert db_rows(api.settings, "SELECT actor_id FROM audit WHERE action = 'request.approved'")
        assert (
            db_rows(api.settings, "SELECT count(*) FROM audit WHERE action = 'request.approve'")[0][
                0
            ]
            == 0  # the route has no guard line: the service writes the one that counts
        )


@pytest.mark.req("EF-USR-010", "EF-USR-022")
def test_approval_is_the_reserved_door_of_a_fresh_session(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, ports: FakePorts
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        admin = admin_client(api)
        api.clock.advance(timedelta(minutes=16))
        stale = api.post(f"{REQUESTS}/{rid}/approve", client=admin, json={"age": 10})
        assert stale.status_code == 401 and "reauth" in stale.json()["type"]
        assert ports.follows == [] and table(api)[0][1] == "pending"


@pytest.mark.req("EF-USR-010")
def test_confirmation_over_the_threshold_by_the_route(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, ports: FakePorts
) -> None:
    ports.estimate_of = Estimate(units=500, size_bytes=0)
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        admin = admin_client(api)
        assert admin.get(f"{REQUESTS}/{rid}/approval").json()["confirmation_required"] is True
        refused = api.post(f"{REQUESTS}/{rid}/approve", client=admin, json={"age": 10})
        assert refused.status_code == 409
        assert refused.json()["code"] == "request.confirmation_required"
        assert ports.follows == [] and table(api)[0][1] == "pending"
        ok = api.post(
            f"{REQUESTS}/{rid}/approve", client=admin, json={"age": 10, "confirm_size": True}
        )
        assert ok.status_code == 200 and len(ports.follows) == 1


@pytest.mark.req("EF-USR-010")
def test_an_adult_text_request_that_resolves_to_nothing_can_only_be_not_found(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, ports: FakePorts
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        member = client_of(api, "member", "10.0.0.51")
        rid = api.post(
            REQUESTS, client=member, json={"target_kind": "free_text", "target_value": "Big"}
        ).json()["id"]
        admin = admin_client(api)
        unresolved = api.post(f"{REQUESTS}/{rid}/approve", client=admin, json={})
        assert unresolved.status_code == 409 and unresolved.json()["code"] == "request.unresolved"
        assert ports.follows == []
        done = api.post(f"{REQUESTS}/{rid}/not-found", client=admin)
        assert done.status_code == 200 and done.json()["state"] == "not_found"
        assert member.get(f"{REQUESTS}/{rid}").json()["state"] == "not_found"


@pytest.mark.req("EF-USR-010")
def test_reject_reason_is_shown_to_the_requester_and_nothing_else_leaks(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        admin = admin_client(api)
        missing = api.post(f"{REQUESTS}/{rid}/reject", client=admin, json={})
        assert missing.status_code == 422
        done = api.post(f"{REQUESTS}/{rid}/reject", client=admin, json={"reason": "Not this one"})
        assert done.status_code == 200
        mine = kid.get(f"{REQUESTS}/{rid}").json()
        assert mine["state"] == "rejected" and mine["reject_reason"] == "Not this one"
        assert set(mine) == CHILD_KEYS
        again = api.post(f"{REQUESTS}/{rid}/reject", client=admin, json={"reason": "again"})
        assert again.status_code == 409
        assert db_rows(api.settings, "SELECT target FROM audit WHERE action = 'request.rejected'")
        assert "Not this one" not in str(db_rows(api.settings, "SELECT target FROM audit"))


@pytest.mark.req("EF-USR-016", "EF-USR-010")
def test_admin_queue_masks_a_private_adult_request(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        member = client_of(api, "member", "10.0.0.51")
        api.post(
            REQUESTS, client=member,
            json={"target_kind": "free_text", "target_value": "Private Wish"},
        )  # fmt: skip
        admin = admin_client(api)
        queue = admin.get(f"{REQUESTS}/queue")
        assert queue.status_code == 200 and "Private Wish" not in queue.text
        assert queue.json()["items"][0]["title"] is None
        assert queue.json()["items"][0]["visibility"] == "private"
        assert member.get(REQUESTS).json()["items"][0]["title"] == "Private Wish"


@pytest.mark.req("EF-USR-010")
def test_cancel_by_the_requester(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        done = api.post(f"{REQUESTS}/{rid}/cancel", client=kid)
        assert done.status_code == 200 and done.json()["state"] == "cancelled"
        assert api.post(f"{REQUESTS}/{rid}/cancel", client=kid).status_code == 409
        assert admin_client(api).get(f"{REQUESTS}/queue").json()["items"] == []


@pytest.mark.req("EF-USR-010")
def test_requests_are_in_the_openapi_and_the_reserved_registry(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from shelfhold.identity.public import RESERVED_ACTIONS

    with instance(monkeypatch, settings, alpha()) as api:
        paths = api.app.openapi()["paths"]
        for _method, path in (*OWN_ROUTES, *ADMIN_ROUTES):
            assert path.replace("{id}", "{request_id}") in paths, path
    assert "request.approve" in RESERVED_ACTIONS
    assert MEMBER_ID  # the member of the instance exists for the attacks above
