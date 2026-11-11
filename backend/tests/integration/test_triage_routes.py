"""L12 (EF-USR-015, EF-EXP-046): the routes behind the "À classer" screen and the "À ranger" view.

Reading the two lists needs an admin session; "classer tout en lot" is the reserved action `rate`
(fresh admin session, never a key, CSRF on the write) and goes through the same `RatingOps` as
the CLI, with the token the list gave back.
"""

from collections.abc import Iterator
from typing import Any

import pytest

from shelfhold.config import Settings
from tests.auth_env import CSRF_HEADER, AuthEnv, auth_env

TOKEN = "1-abcd1234"  # noqa: S105 (test fixture)
ITEMS = [
    {"work_id": "01JWORK0000000000000000001", "title": "<b>Un titre</b>",
     "state": "rating_pending", "reason": "waits"},
]  # fmt: skip


class _Rating:
    def __init__(self) -> None:
        self.batches: list[tuple[str, str, str]] = []

    def listing(self, age: str | None) -> dict[str, Any]:
        return {"age": age, "items": ITEMS, "token": TOKEN if age else None}

    def unshelved(self) -> list[dict[str, str]]:
        return [
            {"edition_id": "01JEDIT0000000000000000001", "work_id": "01JWORK0000000000000000001",
             "title": "Un EPUB", "lang": "fr", "media": "text"},
        ]  # fmt: skip

    def rate_batch(self, age: str, *, confirm: str, principal: Any) -> dict[str, Any]:
        self.batches.append((age, confirm, principal.credential.value))
        return {
            "age": age,
            "rated": [
                {"work_id": ITEMS[0]["work_id"], "target": "work:1", "label": "age.min.13",
                 "effective": 13, "effective_x": False,
                 "withdrawals": [{"edition_id": "E1", "shelf_id": "S1",
                                 "reason": "above_ceiling"}]},
            ],
        }  # fmt: skip


class _Ops:
    def __init__(self) -> None:
        self.rating = _Rating()


@pytest.fixture
def ops() -> _Ops:
    return _Ops()


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        yield env


@pytest.mark.req("EF-USR-015")
def test_to_rate_screen_requires_session_and_csrf(api: AuthEnv, ops: _Ops) -> None:
    for path in ("/api/v1/to-rate", "/api/v1/to-shelve"):
        assert api.client.get(path).status_code == 401  # no session
    body = {"age": "13+", "confirm": "1-abcd1234"}
    assert api.client.post("/api/v1/works/rating-batch", json=body).status_code in (401, 403)
    assert api.login().status_code == 200
    refused = api.client.post("/api/v1/works/rating-batch", json=body)  # session, no CSRF token
    assert refused.status_code == 403
    wrong = api.client.post(
        "/api/v1/works/rating-batch", json=body, headers={CSRF_HEADER: "not-the-token"}
    )
    assert wrong.status_code == 403
    assert ops.rating.batches == []
    done = api.post("/api/v1/works/rating-batch", json=body)
    assert done.status_code == 200
    assert done.json()["rated"][0]["withdrawals"][0]["reason"] == "above_ceiling"
    assert ops.rating.batches == [("13+", "1-abcd1234", "session")]


@pytest.mark.req("EF-USR-015")
def test_to_rate_list_carries_the_token_only_for_an_age(api: AuthEnv) -> None:
    api.login()
    plain = api.client.get("/api/v1/to-rate").json()
    assert plain["token"] is None and plain["items"][0]["state"] == "rating_pending"
    aged = api.client.get("/api/v1/to-rate", params={"age": "13+"}).json()
    assert aged["token"] == TOKEN


@pytest.mark.req("EF-EXP-046")
def test_to_shelve_view_lists_unshelved_editions(api: AuthEnv) -> None:
    api.login()
    body = api.client.get("/api/v1/to-shelve").json()
    assert [i["edition_id"] for i in body["items"]] == ["01JEDIT0000000000000000001"]


@pytest.mark.req("EF-USR-015", "EF-BIB-036")
def test_batch_rating_refused_to_every_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv, ops: _Ops
) -> None:
    from tests.auth_env import run_cli

    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "ci") == 0
    import re

    key = re.search(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}", capsys.readouterr().out)
    assert key
    refused = api.client.post(
        "/api/v1/works/rating-batch",
        json={"age": "13+", "confirm": "x"},
        headers={"Authorization": f"Bearer {key.group(0)}"},
    )
    assert refused.status_code == 403
    assert ops.rating.batches == []


@pytest.mark.req("EF-USR-022", "EF-BIB-036")
def test_batch_rating_needs_a_fresh_session_in_the_default_profile(api: AuthEnv, ops: _Ops) -> None:
    from datetime import timedelta

    api.login()
    api.clock.advance(timedelta(minutes=20))
    stale = api.post("/api/v1/works/rating-batch", json={"age": "0+", "confirm": TOKEN})
    assert stale.status_code == 401 and stale.json()["type"].endswith("/reauth-required")
    assert ops.rating.batches == []  # nothing was rated
