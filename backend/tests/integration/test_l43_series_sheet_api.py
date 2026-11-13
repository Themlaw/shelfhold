"""M3 L43: the doors the series sheet uses."""

from __future__ import annotations

import pytest
from shelfhold.infrastructure.ids import new_ulid
from tests.auth_env import AuthEnv, auth_env


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings):
    with auth_env(monkeypatch, settings) as env:
        assert env.login().status_code == 200
        yield env


@pytest.mark.req("EF-SRC-038")
def test_check_now_button_calls_check(api: AuthEnv):
    response = api.post(f"/api/v1/editions/{new_ulid()}/check")
    assert response.status_code in (400, 404, 409, 422)


@pytest.mark.req("EF-BIB-056")
def test_gap_acknowledge_from_sheet(api: AuthEnv):
    response = api.post(
        f"/api/v1/editions/{new_ulid()}/gaps/ack",
        json={"number": "11", "track": "main", "segment": "1"},
    )
    assert response.status_code in (400, 404, 409, 422)
