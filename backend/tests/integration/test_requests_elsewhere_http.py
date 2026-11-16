"""M4 L30 (EF-USR-019, ENF-SEC-010, D22): the routes of the requests bought or borrowed elsewhere,
through the real application with real sessions, CSRF and keys.

The attacks of the card: a request of someone else is as absent as one that never was, a child
reads no address, a member decides nothing, and a key (even the one of an admin) never acts."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from shelfhold.acquisition.application.requests import SourceRef
from shelfhold.config import Settings
from tests.auth_env import AuthEnv, db_rows
from tests.federated_env import api_key_of, instance
from tests.integration.test_requests_http import (
    REQUESTS,
    admin_client,
    alpha,
    client_of,
    ports,  # noqa: F401  (autouse fixture)
)
from tests.request_env import FakePorts

pytestmark = pytest.mark.req("EF-USR-019", "ENF-SEC-010")

SHOP = "https://shop.example/elise"
ELSEWHERE = ("POST", f"{REQUESTS}/{{id}}/elsewhere")
PROPOSALS = (
    ("POST", f"{REQUESTS}/{{id}}/proposals"),
    ("GET", f"{REQUESTS}/{{id}}/proposals"),
    ("POST", f"{REQUESTS}/{{id}}/proposals/{{pid}}/accept"),
)
ULID = "01ARZ3NDEKTSV4RRFFQ69G5FAV"


def ask(api: AuthEnv, who: TestClient, text: str = "Élise") -> str:
    answer = api.post(REQUESTS, client=who, json={"target_kind": "free_text", "target_value": text})
    assert answer.status_code == 201, answer.text
    return str(answer.json()["id"])


def mark(api: AuthEnv, boss: TestClient, rid: str, **body: Any) -> Any:
    return api.post(
        f"{REQUESTS}/{rid}/elsewhere", client=boss, json={"state": "to_buy", "url": SHOP, **body}
    )


def state(api: AuthEnv, rid: str) -> str:
    return str(db_rows(api.settings, "SELECT state FROM request WHERE id = ?", rid)[0][0])


def test_the_requester_reads_the_address_the_admin_gave(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        member = client_of(api, "member", "10.0.0.42")
        rid = ask(api, member)
        boss = admin_client(api)
        done = mark(api, boss, rid, url=SHOP + "?utm_source=x")
        assert done.status_code == 200, done.text
        assert (done.json()["state"], done.json()["elsewhere_url"]) == ("to_buy", SHOP)
        mine = member.get(f"{REQUESTS}/{rid}").json()
        assert (mine["state"], mine["elsewhere_url"]) == ("to_buy", SHOP)
        bad = mark(api, boss, rid, url="javascript:alert(1)")
        assert bad.status_code in (400, 422) and "request.invalid_link" in bad.text
        assert state(api, rid) == "to_buy"


def test_a_request_of_someone_else_is_as_absent_as_one_that_never_was(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        member = client_of(api, "member", "10.0.0.42")
        rid = ask(api, member)
        mark(api, admin_client(api), rid)
        kid = client_of(api, "kid", "10.0.0.41")
        for method, path in (("GET", f"{REQUESTS}/{rid}"), ("POST", f"{REQUESTS}/{rid}/cancel")):
            absent = kid.get if method == "GET" else lambda u: api.post(u, client=kid)
            theirs = absent(path)
            never = absent(path.replace(rid, ULID))
            assert theirs.status_code == never.status_code == 404
            assert theirs.json().get("code") == never.json().get("code")
            assert SHOP not in theirs.text and "lise" not in theirs.text
        assert state(api, rid) == "to_buy"


def test_a_child_reads_no_address_and_only_the_word_waiting(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    from tests.integration.test_requests_http import ask_as_child, family

    with instance(monkeypatch, settings, alpha()) as api:
        kid = family(api)
        rid = ask_as_child(api, kid)
        assert mark(api, admin_client(api), rid).status_code == 200
        body = kid.get(f"{REQUESTS}/{rid}").json()
        assert body["elsewhere_url"] is None and SHOP not in str(body)
        assert body["family_state"] == "waiting"
        assert SHOP not in kid.get(REQUESTS).text


def test_nobody_but_an_admin_session_acts_elsewhere(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    ports: FakePorts,  # noqa: F811
) -> None:
    ports.search_hits = [SourceRef("alpha", "r1", "Result", {"entry": "e1"})]
    with instance(monkeypatch, settings, alpha()) as api:
        member = client_of(api, "member", "10.0.0.42")
        kid = client_of(api, "kid", "10.0.0.41")
        rid = ask(api, member)
        keys = {
            "kid": api_key_of(api, "kid"),
            "member": api_key_of(api, "member"),
            "admin read-only": api_key_of(api, "admin"),
            "admin": api_key_of(api, "admin", read_only=False),
        }
        before = db_rows(api.settings, "SELECT id, state, target_kind FROM request")
        for method, path in (ELSEWHERE, *PROPOSALS):
            url = path.replace("{id}", rid).replace("{pid}", ULID)
            body = {"state": "to_buy", "url": SHOP}
            for who, client in (("member", member), ("kid", kid)):
                answer = (
                    client.get(url) if method == "GET" else api.post(url, client=client, json=body)
                )
                assert answer.status_code == 403, (who, method, path, answer.text)
            for owner, key in keys.items():
                bare = api.other_client("10.9.9.9")
                answer = bare.request(
                    method, url, headers={"Authorization": f"Bearer {key}"},
                    **({"json": body} if method == "POST" else {}),
                )  # fmt: skip
                assert answer.status_code == 403, (owner, method, path, answer.text)
        assert db_rows(api.settings, "SELECT id, state, target_kind FROM request") == before
        assert ports.searches == []


def test_the_admin_searches_and_accepts_a_proposal_on_a_request_of_a_member(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    ports: FakePorts,  # noqa: F811
) -> None:
    ports.search_hits = [
        SourceRef("alpha", f"r{i}", f"Result {i}", {"entry": f"e{i}"}) for i in range(7)
    ]
    with instance(monkeypatch, settings, alpha()) as api:
        member = client_of(api, "member", "10.0.0.42")
        rid = ask(api, member, "Un titre sans réponse")
        boss = admin_client(api)
        found = api.post(f"{REQUESTS}/{rid}/proposals", client=boss, json={})
        assert found.status_code == 200, found.text
        items = found.json()["items"]
        assert len(items) == 5 and set(items[0]) == {"id", "source_key", "title"}
        assert ports.searches == ["Un titre sans réponse"]
        done = api.post(f"{REQUESTS}/{rid}/proposals/{items[0]['id']}/accept", client=boss, json={})
        assert done.status_code == 200, done.text
        assert state(api, rid) == "pending"
        kinds = db_rows(api.settings, "SELECT target_kind FROM request WHERE id = ?", rid)
        assert kinds == [("source_ref",)]
