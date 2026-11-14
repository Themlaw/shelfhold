"""M3 L42 (EF-UX-006, EF-BIB-030): what the « Ajouter à ma bibliothèque » sheet needs of the server.

The sheet shows, before anything is written, how many Units a choice would bring, their size and
the free space, for the three modes: everything (`all`), from a number (`from_number`, with
`from_number=`) and the news only (`new_only`). The figures are the admin's: a member, a child and
a key of either are refused. `POST /api/v1/follows` takes the same choice (`mode`, `from_number`)
and the shelf (`shelf_id`), and refuses what it does not understand before anything is sent.
"""

from __future__ import annotations

import pytest

from shelfhold.config import Settings
from tests.auth_env import CSRF_HEADER, PASSWORD
from tests.federated_env import FakeSource, api_key_of, hit, instance

ESTIMATE = "/api/v1/sources/alpha/series/a1/estimate"
FOLLOWS = "/api/v1/follows"
FIGURES = {"mode", "units", "size_bytes", "free_bytes", "needs_confirmation"}


def alpha() -> FakeSource:
    return FakeSource("alpha", name="Alpha texts", pages=[[hit("a1", "A Quiet Tale")]])


@pytest.mark.req("EF-UX-006")
@pytest.mark.parametrize("mode", ["all", "new_only"])
def test_add_estimate_answers_units_size_and_free_space(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, mode: str
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        assert api.login().status_code == 200
        answer = api.client.get(ESTIMATE, params={"mode": mode})
        assert answer.status_code == 200, answer.text
        body = answer.json()
        assert set(body) == FIGURES
        assert body["mode"] == mode
        for name in ("units", "size_bytes", "free_bytes"):
            assert isinstance(body[name], int) and not isinstance(body[name], bool), name
            assert body[name] >= 0, name
        assert isinstance(body["needs_confirmation"], bool)


@pytest.mark.req("EF-UX-006")
def test_add_estimate_from_a_number_is_never_larger_than_everything(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        assert api.login().status_code == 200
        everything = api.client.get(ESTIMATE, params={"mode": "all"}).json()
        later = api.client.get(ESTIMATE, params={"mode": "from_number", "from_number": "5"})
        assert later.status_code == 200, later.text
        assert later.json()["mode"] == "from_number"
        assert later.json()["units"] <= everything["units"]
        assert later.json()["size_bytes"] <= everything["size_bytes"]


@pytest.mark.req("EF-UX-006")
@pytest.mark.parametrize(
    "params",
    [
        {"mode": "bogus"},
        {"mode": "from_number"},  # the number is the whole point of this mode
        {"mode": "from_number", "from_number": ""},
        {"mode": "from_number", "from_number": "x" * 65},
        {"mode": "all", "from_number": "5"},  # a number with a mode that has none: not guessed
    ],
)
def test_add_estimate_refuses_a_choice_it_does_not_understand(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, params: dict[str, str]
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        assert api.login().status_code == 200
        assert api.client.get(ESTIMATE, params=params).status_code == 422


@pytest.mark.req("EF-UX-006")
def test_add_estimate_of_an_unknown_source_is_a_problem_never_a_500(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        assert api.login().status_code == 200
        gone = api.client.get("/api/v1/sources/nowhere/series/a1/estimate", params={"mode": "all"})
        assert gone.status_code in (404, 409), gone.text
        assert gone.headers["content-type"].startswith("application/problem+json")


@pytest.mark.req("EF-UX-006", "ENF-SEC-010")
def test_add_estimate_is_the_admins_only(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        params = {"mode": "all"}
        assert api.other_client("10.0.0.50").get(ESTIMATE, params=params).status_code in (401, 403)
        for who, ip in (("member", "10.0.0.51"), ("kid", "10.0.0.52")):
            client = api.other_client(ip)
            assert api.login(who, PASSWORD, client=client).status_code == 200
            assert client.get(ESTIMATE, params=params).status_code == 403, who
            key = api_key_of(api, who)
            keyed = api.other_client(f"10.0.1.{ip[-2:]}")
            assert (
                keyed.get(ESTIMATE, params=params, headers={"Authorization": f"Bearer {key}"}).status_code == 403
            ), who


@pytest.mark.req("EF-UX-006")
@pytest.mark.parametrize(
    "body",
    [
        {"mode": "bogus"},
        {"mode": "from_number"},  # no number
        {"mode": "all", "from_number": "5"},
        {"mode": "from_number", "from_number": "5", "shelf_id": ""},
        {"shelf_id": 7},
        {"confirm_units": -1},
        {"confirm_bytes": "lots"},
    ],
)
def test_follow_refuses_a_choice_it_does_not_understand_before_sending_anything(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, body: dict[str, object]
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        assert api.login().status_code == 200
        answer = api.client.post(
            FOLLOWS,
            json={"source_key": "alpha", "remote_id": "a1", **body},
            headers={CSRF_HEADER: api.csrf()},
        )
        assert answer.status_code == 422, answer.text


@pytest.mark.req("EF-UX-006", "ENF-SEC-010")
def test_follow_with_a_choice_is_still_the_admins_only(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with instance(monkeypatch, settings, alpha()) as api:
        for who, ip in (("member", "10.0.0.53"), ("kid", "10.0.0.54")):
            client = api.other_client(ip)
            assert api.login(who, PASSWORD, client=client).status_code == 200
            answer = client.post(
                FOLLOWS,
                json={"source_key": "alpha", "remote_id": "a1", "mode": "new_only"},
                headers={CSRF_HEADER: api.csrf(client)},
            )
            assert answer.status_code == 403, who
