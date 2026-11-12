"""`GET /api/v1/search?scope=all` (M3 L18, EF-SRC-005, EF-UX-011, EF-USR-014): the federated search
behind the real route, the session and the roles. Admin only: a child or a member, with a session or
a key, is refused before one source is asked (the family search of L19 is the child's door)."""

from __future__ import annotations

import pytest

from shelfhold.config import Settings
from tests.auth_env import PASSWORD
from tests.federated_env import FakeSource, api_key_of, hit, instance

ALL = {"scope": "all", "q": "hobbit"}


def sources() -> tuple[FakeSource, FakeSource]:
    alpha = FakeSource(
        "alpha", name="Alpha texts", kinds=("text",),
        pages=[
            [hit("a1", "The Hobbit", isbn="1"), hit("a2", "Unrated Shocker")],
            [hit("a3", "Third")],
        ],
    )  # fmt: skip
    beta = FakeSource(
        "beta", name="Beta scans", kinds=("images",), pages=[[hit("b1", "Der Hobbit", isbn="1")]]
    )
    return alpha, beta


@pytest.mark.req("EF-SRC-005", "EF-UX-011")
def test_federated_route_groups_pages_and_reports_where_it_looked(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        assert api.login().status_code == 200
        first = api.client.get("/api/v1/search", params=ALL)
        assert first.status_code == 200, first.text
        body = first.json()
        hobbit, shocker = body["items"]
        assert [e["source_key"] for e in hobbit["editions"]] == [
            "alpha",
            "beta",
        ]  # one Work, two Editions
        assert set(hobbit["editions_by_format"]) == {"text", "images"}
        assert shocker["title"] == "Unrated Shocker"
        assert [(s["source_key"], s["status"]) for s in body["searched_in"]] == [
            ("alpha", "ok"),
            ("beta", "ok"),
        ]
        trust = body["searched_in"][0]["trust"]
        assert trust["level"] == "official" and [x["level"] for x in trust["lines"]] == [
            "official", "community", "unknown",
        ]  # fmt: skip
        assert body["state"] == "results" and body["next_cursor"]
        second = api.client.get("/api/v1/search", params={**ALL, "cursor": body["next_cursor"]})
        assert second.status_code == 200 and [g["title"] for g in second.json()["items"]] == [
            "Third"
        ]
        assert second.json()["next_cursor"] is None
        assert [g["group_id"] for g in body["items"]] == ["g1", "g2"]


@pytest.mark.req("EF-SRC-005")
def test_federated_route_refuses_forged_cursors_and_bad_input_without_500(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        assert api.login().status_code == 200
        token = api.client.get("/api/v1/search", params=ALL).json()["next_cursor"]
        asked = len(alpha.calls)
        for bad in ("", "x", token[:-1], token + "x", "A" * 5000, "%00", "é"):
            response = api.client.get("/api/v1/search", params={**ALL, "cursor": bad})
            assert response.status_code in (400, 422), (bad, response.text)
            assert "Traceback" not in response.text
        other_query = api.client.get("/api/v1/search", params={**ALL, "q": "else", "cursor": token})
        assert other_query.status_code == 400 and other_query.json()["type"].endswith(
            "invalid-cursor"
        )
        assert len(alpha.calls) == asked  # none of them reached a source
        for params in (
            {"scope": "all"},
            {"scope": "all", "q": "  "},
            {"scope": "everything", "q": "x"},
            {"scope": "federated", "q": "x"},
        ):
            assert api.client.get("/api/v1/search", params=params).status_code in (400, 422), params


@pytest.mark.req("EF-SRC-005", "EF-UX-011")
def test_only_the_switched_on_sources_are_searched(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta, active=["alpha"]) as api:
        assert api.login().status_code == 200
        body = api.client.get("/api/v1/search", params=ALL).json()
        assert [s["source_key"] for s in body["searched_in"]] == ["alpha"] and beta.calls == []


@pytest.mark.req("EF-UX-011")
def test_federated_route_with_no_source_switched_on_is_the_honest_empty_state(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    gamma = FakeSource("gamma", pages=[[hit("g1", "Hobbit")]])
    with instance(monkeypatch, settings, gamma, active=[]) as api:
        assert api.login().status_code == 200
        body = api.client.get("/api/v1/search", params=ALL).json()
        assert body["state"] == "no_sources" and body["action"] == "request" and body["items"] == []
        assert gamma.calls == []


@pytest.mark.req("EF-SRC-005")
def test_federated_route_admin_key_may_search(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        key = api_key_of(api, "admin")
        found = api.other_client("10.0.0.30").get(
            "/api/v1/search", params=ALL, headers={"Authorization": f"Bearer {key}"}
        )
        assert found.status_code == 200 and found.json()["items"]


@pytest.mark.req("EF-USR-014", "EF-SRC-005")
def test_child_and_member_never_reach_the_federated_search(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The attack: a child (or a member) asks `scope=all`, by session or by key, with a cursor of
    the admin's or without. Nothing is asked of a source, and not one remote title (rated above the
    ceiling of the child, or not rated at all) comes back."""
    alpha, beta = sources()
    with instance(monkeypatch, settings, alpha, beta) as api:
        assert api.login().status_code == 200
        admin_cursor = api.client.get("/api/v1/search", params=ALL).json()["next_cursor"]
        asked = (len(alpha.calls), len(beta.calls))
        for index, username in enumerate(("kid", "member")):
            client = api.other_client(f"10.0.0.{40 + index}")
            assert api.login(username, PASSWORD, client=client).status_code == 200
            key = api_key_of(api, username)
            attempts = [
                client.get("/api/v1/search", params=ALL),
                client.get("/api/v1/search", params={**ALL, "cursor": admin_cursor}),
                client.get("/api/v1/search", params={"scope": "all", "q": "Unrated Shocker"}),
                api.other_client(f"10.0.0.{50 + index}").get(
                    "/api/v1/search", params=ALL, headers={"Authorization": f"Bearer {key}"}
                ),
            ]
            for response in attempts:
                assert response.status_code == 403, (username, response.text)
                assert "Hobbit" not in response.text and "Shocker" not in response.text
                assert "searched_in" not in response.text and "editions" not in response.text
        anonymous = api.other_client("10.0.0.60").get("/api/v1/search", params=ALL)
        assert anonymous.status_code in (401, 403) and "Hobbit" not in anonymous.text
        assert (len(alpha.calls), len(beta.calls)) == asked  # no source was asked for any of them
