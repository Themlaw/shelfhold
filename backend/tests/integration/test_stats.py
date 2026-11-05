"""`shelfhold stats --since 30d` and `GET /api/v1/stats` (EF-INT-025, culpable in M1a).

Thirty simulated days over a real migrated database: the indicators show, nothing leaves the
instance, only the admin reads them.
"""

import json
import socket
import sqlite3
from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
from tests.auth_env import PASSWORD, T0, AuthEnv, auth_env, db_rows, run_cli
from tests.stats_env import seed_thirty_days


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings) as env:
        yield env


@pytest.fixture(autouse=True)
def no_outbound_connection(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    attempts: list[object] = []

    def refuse(*args: object, **kwargs: object) -> None:
        attempts.append(args)
        raise AssertionError("stats must not open a connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    return attempts


def _member_key(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
) -> str:
    (admin_hash,) = db_rows(api.settings, "SELECT password_hash FROM user WHERE role = 'admin'")[0]
    connection = sqlite3.connect(api.settings.database_path)
    try:
        connection.execute(
            "INSERT INTO user (id, username, password_hash, role, can_rate) "
            "VALUES ('01J0000000000000000000MEMB', 'member', ?, 'member', 0)",
            (admin_hash,),
        )
        connection.commit()
    finally:
        connection.close()
    capsys.readouterr()
    assert (
        run_cli(
            monkeypatch, api.settings, "admin", "keys", "create", "--name", "kid",
            "--username", "member",
        )
        == 0
    )  # fmt: skip
    out = capsys.readouterr().out
    return next(word for word in out.split() if word.startswith("shk_"))


@pytest.mark.req("EF-INT-025")
def test_stats_local_only_admin_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    no_outbound_connection: list[object],
) -> None:
    seed_thirty_days(api.settings, now=T0)
    api.clock.current = T0

    # admin session: every indicator is there
    assert api.login().status_code == 200
    response = api.client.get("/api/v1/stats", params={"since": "30d"})
    assert response.status_code == 200
    body = response.json()
    assert body["since_days"] == 30
    assert body["human_interventions"] == {"waiting_states": 6, "needs_review_replays": 2}
    assert body["units_exported"] == 30
    assert body["median_detection_to_visible_s"] == 3600
    assert body["guards_triggered"] == {"sentinel": 1, "foreign_writer": 2}

    # a member's key and a member's session: 403, and no number leaks
    key = _member_key(monkeypatch, capsys, api)
    denied = api.other_client("10.0.0.9").get(
        "/api/v1/stats", headers={"Authorization": f"Bearer {key}"}
    )
    assert denied.status_code == 403
    assert "units_exported" not in denied.text
    member = api.other_client("10.0.0.10")
    assert api.login("member", PASSWORD, client=member).status_code == 200
    assert member.get("/api/v1/stats").status_code == 403
    # nobody at all: refused before reading anything
    assert api.other_client("10.0.0.11").get("/api/v1/stats").status_code in (401, 403)

    # the CLI shows the same figures, straight from the database
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "stats", "--since", "30d", "--json") == 0
    assert json.loads(capsys.readouterr().out) == body

    assert no_outbound_connection == []  # EF-INT-025, ENF-PRIV-001: no telemetry, no request


@pytest.mark.req("EF-INT-025")
def test_stats_window_excludes_older_activity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    seed_thirty_days(api.settings, now=T0)
    assert run_cli(monkeypatch, api.settings, "stats", "--since", "10d", "--json") == 0
    short = json.loads(capsys.readouterr().out)
    assert short["since_days"] == 10
    assert 0 < short["units_exported"] < 30
    assert run_cli(monkeypatch, api.settings, "stats", "--since", "banana") != 0
