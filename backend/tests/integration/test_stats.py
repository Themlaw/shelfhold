"""`shelfhold stats --since 30d` and `GET /api/v1/stats` (EF-INT-025, culpable in M1a).

Thirty simulated days over a real migrated database: the indicators show, nothing leaves the
instance, only the admin reads them.
"""

import json
import socket
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from shelfhold.config import Settings
from shelfhold.identity.public import local_auth
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import PASSWORD, AuthEnv, auth_env, db_rows, run_cli
from tests.follow_env import follow_bench
from tests.stats_env import Delivery, seed_activity


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


def _add_member(api: AuthEnv) -> None:
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


def _key_of(api: AuthEnv, username: str, *, read_only: bool = False) -> str:
    engine = create_db_engine(api.settings.database_path)
    try:
        created = local_auth(engine, api.settings).create_api_key(
            name="kid", username=username, read_only=read_only
        )
    finally:
        engine.dispose()
    return created.value


@pytest.mark.req("EF-INT-025")
def test_stats_local_only_admin_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    api: AuthEnv,
    no_outbound_connection: list[object],
) -> None:
    now = datetime.now(UTC)  # the CLI reads the system clock; the windows have an hour of margin
    seed_activity(api.settings, now=now)
    api.clock.current = now

    # admin session: every indicator is there
    assert api.login().status_code == 200
    response = api.client.get("/api/v1/stats", params={"since": "30d"})
    assert response.status_code == 200
    body = response.json()
    assert body["since_days"] == 30
    assert body["human_interventions"] == {"waiting_states": 6, "needs_review_replays": 2}
    assert body["units_exported"] == 0  # nothing was exported on this instance
    assert body["median_detection_to_visible_s"] is None
    assert body["arrivals_pending"] == 0
    assert body["guards_triggered"] == {"sentinel": 1, "foreign_writer": 2}

    # an admin key reads the same figures
    admin_key = _key_of(api, "admin")
    assert (
        api.other_client("10.0.0.8")
        .get("/api/v1/stats", headers={"Authorization": f"Bearer {admin_key}"})
        .json()
        == body
    )

    # a member's key and a member's session: 403, and no number leaks
    _add_member(api)
    key = _key_of(api, "member", read_only=True)  # the `admin` scope is not a member's to hold
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


def _stats(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    delivery: Delivery,
    since: str,
) -> dict[str, object]:
    """`shelfhold stats --json` over the database the whole chain wrote."""
    database = delivery.bench.lib.engine.url.database
    assert database is not None
    here = settings.model_copy(update={"data_dir": Path(database).parent})
    capsys.readouterr()
    assert run_cli(monkeypatch, here, "stats", "--since", since, "--json") == 0
    return dict(json.loads(capsys.readouterr().out))


@pytest.mark.req("EF-INT-025")
def test_stats_median_delay_detection_to_kavita_resolved(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    """Detection by the follow cycle, export by the pipeline, arrival seen by the polling against
    the simulated Kavita: the delay is the one the clock ran between them, not the write time."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        delivery = Delivery(bench)
        bench.clock.set(datetime.now(UTC) - timedelta(hours=3))
        delivery.baseline()
        detected = bench.clock.now()
        delivery.new_chapter(3)
        bench.clock.advance(seconds=600)
        rel_paths = delivery.export()
        assert len(rel_paths) == 1  # the chapter was written
        bench.clock.advance(seconds=1200)  # Kavita takes its time to scan
        delivery.kavita_indexes(rel_paths)
        before_poll = delivery.poll()
        assert before_poll.indexed == rel_paths
        stats = _stats(monkeypatch, capsys, settings, delivery, "30d")
        assert stats["median_detection_to_visible_s"] == 1800
        assert stats["units_exported"] == len(rel_paths)
        assert stats["arrivals_pending"] == 0  # all of them were seen
        assert bench.clock.now() - detected == timedelta(seconds=1800)


@pytest.mark.req("EF-INT-025")
def test_stats_no_visibility_before_the_polling_found_the_file(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    """Written is not visible: without the polling's confirmation there is no delay to report."""
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        delivery = Delivery(bench)
        bench.clock.set(datetime.now(UTC) - timedelta(hours=3))
        delivery.baseline()
        delivery.new_chapter(3)
        bench.clock.advance(seconds=600)
        assert delivery.export()
        stats = _stats(monkeypatch, capsys, settings, delivery, "30d")
        assert stats["median_detection_to_visible_s"] is None
        assert stats["units_exported"] > 0
        assert stats["arrivals_pending"] == stats["units_exported"]  # written, still waiting


@pytest.mark.req("EF-INT-025")
def test_stats_window_excludes_older_activity(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        delivery = Delivery(bench)
        now = datetime.now(UTC)
        bench.clock.set(now - timedelta(days=20))  # a first delivery, long ago
        delivery.baseline()
        delivery.new_chapter(3)
        old = delivery.export()
        delivery.kavita_indexes(old)
        assert delivery.poll().indexed == old
        bench.clock.set(now - timedelta(days=2))  # a chapter arrives later
        delivery.new_chapter(4)
        everything = delivery.export()
        recent = [rel for rel in everything if rel not in old]
        assert recent
        delivery.kavita_indexes(recent)
        assert delivery.poll().indexed == recent
        short = _stats(monkeypatch, capsys, settings, delivery, "10d")
        assert short["since_days"] == 10
        assert short["units_exported"] == len(recent)
        assert _stats(monkeypatch, capsys, settings, delivery, "30d")["units_exported"] == len(
            everything
        )
        assert run_cli(monkeypatch, settings, "stats", "--since", "banana") != 0
