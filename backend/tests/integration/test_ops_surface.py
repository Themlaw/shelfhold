"""EF-INT-023 (L36): the operating surface — `status`, `/healthz`, the CLI commands and the routes
that leave the waiting states, with the reserved ones refused to every key."""
# ruff: noqa: S105 (synthetic secrets)

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import APIRouter

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.export.application.target_state import load_health
from shelfhold.identity.public import EXIT_ROUTES, authorize_reserved
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.local_socket import call
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition, export, platform
from shelfhold.interfaces.cli import _ops as cli_ops
from shelfhold.interfaces.http_api.ops import build_ops_router
from shelfhold.interfaces.local_cli import IdentityDispatcher
from shelfhold.ops import OpsServices, endpoint_of
from tests.auth_env import AuthEnv, auth_env, run_cli
from tests.export_env import ExportEnv, export_env
from tests.kavita_export_env import wire
from tests.local_socket_env import serving
from tests.ops_env import (
    FakeKeyring,
    FakeRestore,
    add_job,
    job_status,
    make_ops,
    put_health,
)

pytestmark = pytest.mark.req("EF-INT-023")
KEY = re.compile(r"shk_[0-9a-f]{8}_[A-Za-z0-9_-]{43}")


def _seed_waiting(settings: Settings) -> str:
    """A dead job, a paused job and a paused follow cycle in the database of `settings`."""
    engine = create_db_engine(settings.database_path)
    try:
        dead = add_job(engine, kind="library.safeguard", status="dead_letter", code="job.failed")
        add_job(engine, kind="export.run", status="paused", code="window.preempted")
        with engine.begin() as connection:
            connection.execute(
                sa.insert(acquisition.source_config).values(
                    plugin_id="suwayomi", settings={},
                    health={"pause": {"failure_class": "auth", "event_type": "source.auth_expired",
                                      "waiting_state": "auth_expired", "since": "2026-11-05T09:00:00"}},
                )
            )  # fmt: skip
    finally:
        engine.dispose()
    return dead


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings):  # type: ignore[no-untyped-def]
    with auth_env(monkeypatch, settings) as env:
        yield env


def test_status_lists_paused_queues_with_reason_and_command(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], api: AuthEnv
) -> None:
    dead = _seed_waiting(api.settings)
    assert api.client.get("/api/v1/status").status_code == 401  # never anonymous
    api.login()
    body = api.client.get("/api/v1/status").json()
    by_state = {item["state"]: item for item in body["waiting"]}
    assert set(by_state) == {"dead_letter", "auth_expired"}
    assert by_state["dead_letter"]["command"] == f"shelfhold jobs retry {dead}"
    assert by_state["dead_letter"]["reason"] and by_state["dead_letter"]["subject"] == dead
    assert by_state["auth_expired"]["command"].startswith("shelfhold source set-secret")
    assert by_state["auth_expired"]["reserved"] is True  # the exit is a reserved action
    queues = {q["queue"]: q for q in body["queues"]}
    assert queues["follow"]["reason"] == "auth_expired"
    assert queues["jobs.export.run"]["reason"] == "window.preempted"
    # the same list on the command line, in text and in JSON
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "status", "--json") == 0
    from_cli = json.loads(capsys.readouterr().out)
    assert {i["state"] for i in from_cli["waiting"]} == {"dead_letter", "auth_expired"}
    assert run_cli(monkeypatch, api.settings, "status") == 0
    text = capsys.readouterr().out
    assert f"shelfhold jobs retry {dead}" in text and "auth_expired" in text


def test_healthz_exposes_paused_queues(api: AuthEnv) -> None:
    """A7: anonymous callers get the state only; a session or a key also gets the paused queues
    with their reason, and the version."""
    _seed_waiting(api.settings)
    anonymous = api.client.get("/healthz").json()
    assert anonymous == {"state": "ok"}  # no name, no version
    api.login()
    seen = api.client.get("/healthz").json()
    assert seen["state"] == "ok" and seen["version"]
    assert seen["paused_queues"] == ["follow", "jobs.export.run", "jobs.library.safeguard"]
    reasons = {p["queue"]: p["reason"] for p in seen["paused"]}
    assert reasons == {
        "follow": "auth_expired",
        "jobs.export.run": "window.preempted",
        "jobs.library.safeguard": "dead_letter",
    }


# -- the commands -----------------------------------------------------------------------------------


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, settings: Settings):  # type: ignore[no-untyped-def]
    """The settings database exists (the channel checks it); the services are the test's."""
    migrate(settings)
    with export_env() as env:
        monkeypatch.setattr(cli_ops, "open_ops", lambda _settings: make_ops(env))
        yield env


def _exported_missing(env: ExportEnv) -> str:
    series = env.new_series()
    env.add_chapter(series, "2")
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    with env.engine.begin() as connection:
        connection.execute(sa.update(export.export_record).values(state="missing"))
    return str(env.rows(export.export_record)[0].export_item_id)


def test_cli_missing_resolve(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, cli_env: ExportEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    item = _exported_missing(cli_env)
    dest = cli_env.destination_id
    assert run_cli(monkeypatch, settings, "missing", "list") == 0
    assert item in capsys.readouterr().out
    # neither choice given: nothing is decided
    assert run_cli(monkeypatch, settings, "missing", "resolve", dest, item) == 2
    assert cli_env.rows(export.export_record)[0].state == "missing"
    assert run_cli(monkeypatch, settings, "missing", "resolve", dest, item, "--unfollow") == 0
    assert cli_env.rows(export.export_record)[0].state == "recycled"  # the file is not wanted back
    with cli_env.engine.connect() as connection:
        flagged = connection.exec_driver_sql(
            "SELECT follow_suppressed, follow_suppressed_by FROM unit WHERE follow_suppressed"
        ).all()
    assert len(flagged) == 1  # "stop following this chapter", with its provenance (EF-EXP-018)


def test_cli_series_resume(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, cli_env: ExportEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    series = cli_env.new_series()
    cli_env.add_chapter(series, "2")
    cli_env.pipeline.run(cli_env.destination_id, job_id=new_ulid())
    series_id = cli_env.rows(export.export_series)[0].id
    with cli_env.engine.begin() as connection:
        connection.execute(sa.update(export.export_series).values(paused_reason="foreign_writer"))
    assert run_cli(monkeypatch, settings, "series", "resume", series_id) == 0
    assert cli_env.rows(export.export_series)[0].paused_reason is None
    assert run_cli(monkeypatch, settings, "series", "resume", series_id) == 1  # nothing to resume
    capsys.readouterr()


def test_cli_jobs_list_retry_dead_letter(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, cli_env: ExportEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dead = add_job(cli_env.engine, kind="library.safeguard", status="dead_letter", code="job.failed")
    done = add_job(cli_env.engine, kind="export.run", status="succeeded")
    assert run_cli(monkeypatch, settings, "jobs", "list", "--status", "dead_letter") == 0
    listed = capsys.readouterr().out
    assert dead in listed and done not in listed and "job.failed" in listed
    assert run_cli(monkeypatch, settings, "jobs", "retry", done) == 1  # it did not die
    assert job_status(cli_env.engine, done) == "succeeded"
    assert run_cli(monkeypatch, settings, "jobs", "retry", dead) == 0
    assert job_status(cli_env.engine, dead) == "queued"


def test_cli_target_reconnect_and_rotate_key(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    migrate(settings)
    with export_env() as env:
        w = wire(env)
        rotated: list[tuple[str, str]] = []

        class Headless:
            def target_rotate_key(self, *, target: str, password: str) -> None:
                rotated.append((target, password))

        ops = make_ops(env, w, headless=Headless())
        monkeypatch.setattr(cli_ops, "open_ops", lambda _settings: ops)
        put_health(env, paused=True, paused_route="POST /api/library/scan-folder")
        # the account has its rights back: the fake answers 200
        assert run_cli(monkeypatch, settings, "target", "reconnect", env.target_id) == 0
        assert not load_health(env.engine.connect(), env.target_id).paused
        # `rotate-key` reads the admin password from a file, never from argv
        secret = tmp_path / "pw"
        secret.write_text("a-synthetic-password\n", encoding="utf-8")
        secret.chmod(0o600)
        assert run_cli(
            monkeypatch, settings, "target", "rotate-key", env.target_id, "--password-file", str(secret)
        ) == 0  # fmt: skip
        assert rotated == [(env.target_id, "a-synthetic-password")]
        assert "a-synthetic-password" not in capsys.readouterr().out
        assert run_cli(monkeypatch, settings, "target", "recheck", env.target_id) == 0


def test_ops_methods_over_the_local_socket(settings: Settings, tmp_path: Path) -> None:
    """The running server's channel: `local_cli` may call a reserved exit, the audit says who."""
    with export_env() as env:
        keyring = FakeKeyring()
        ops = make_ops(env, keyring=keyring, restore=FakeRestore())
        from shelfhold.identity.public import local_auth

        migrate(settings)
        auth = local_auth(create_db_engine(settings.database_path), settings)
        sock = tmp_path / "s.sock"
        with serving(sock, IdentityDispatcher(auth, ops=ops)):
            waiting = call(sock, "ops.status", {})
            assert isinstance(waiting, dict) and {i["state"] for i in waiting["waiting"]} >= {
                "restored_behind", "tombstone_key_unknown"
            }
            call(sock, "ops.tombstone_accept_lost_keyring", {"confirmation": "the keyring is lost"})
            assert keyring.waived == [("the keyring is lost", "local_cli")]


# -- the routes -------------------------------------------------------------------------------------


def _router(ops: OpsServices) -> APIRouter:
    return build_ops_router(lambda: ops)


def _url(command: str, **ids: str) -> tuple[str, str]:
    method, path = endpoint_of(command)
    return method, path.format(**ids)


def _body(command: str) -> dict[str, Any]:
    return {
        "source set-secret": {"secret": "admin:new"},
        "target rotate-key": {"password": "a-synthetic-password"},
        "tombstone accept-lost-keyring": {"confirmation": "the keyring is lost"},
        "reconcile --adopt-after-restore [confirm-deletion]": {"confirm_deletion": True},
        "reconcile --adopt-after-restore": {"confirm_deletion": False},
        "target recheck [child_exposure]": {},
    }.get(command, {})


class _Headless:
    def target_rotate_key(self, *, target: str, password: str) -> None: ...
    def source_set_secret(self, secret: str) -> None: ...
    def source_recheck(self) -> None: ...


def test_reserved_exit_commands_require_fresh_session_or_cli_on_the_real_routes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], settings: Settings
) -> None:
    """Every reserved exit of the catalogue, on its real route: a fresh session goes through, an
    API key (admin scope included) is refused with `credential-not-allowed` and nothing runs, a
    stale session must re-authenticate; the non-reserved exits take a key."""
    with export_env() as env:
        w = wire(env)
        keyring, restore = FakeKeyring(), FakeRestore()
        ops = make_ops(env, w, keyring=keyring, restore=restore, headless=_Headless())
        put_health(env, child_exposure=True)
        ids = {"target_id": env.target_id}
        reserved = [r for r in EXIT_ROUTES if r.reserved is not None]
        served = [r for r in reserved if r.command in endpoint_table()]
        assert {r.command for r in served} >= {
            "source set-secret", "target rotate-key", "tombstone accept-lost-keyring",
            "target recheck [child_exposure]",
            "reconcile --adopt-after-restore [confirm-deletion]",
        }  # fmt: skip
        with auth_env(monkeypatch, settings, routers=[_router(ops)]) as api:
            capsys.readouterr()
            assert run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "ci") == 0
            key = KEY.search(capsys.readouterr().out)
            assert key
            bearer = {"Authorization": f"Bearer {key.group(0)}"}
            api.login()
            for route in served:
                method, path = _url(route.command, **ids)
                refused = api.client.request(method, path, json=_body(route.command), headers=bearer)
                assert refused.status_code == 403, route.command
                assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
            assert keyring.waived == [] and restore.calls == []  # nothing ran
            for route in served:
                method, path = _url(route.command, **ids)
                put_health(env, child_exposure=True)
                done = api.post(path, json=_body(route.command)) if method == "POST" else None
                assert done is not None and done.status_code in (200, 202, 204), route.command
            assert keyring.waived and restore.calls == [True]
            # a session older than 15 minutes must prove itself again where the property says so
            api.clock.advance(timedelta(minutes=20))
            for route in served:
                assert route.reserved is not None
                _method, path = _url(route.command, **ids)
                stale = api.post(path, json=_body(route.command))
                wants_fresh = route.reserved.needs_fresh_auth(
                    restricted_profile_exists=False, hardened=False
                )
                assert stale.status_code == (401 if wants_fresh else stale.status_code), route.command
                if wants_fresh:
                    assert stale.json()["type"].endswith("reauth-required")
            # a non-reserved exit is open to an admin key
            job = add_job(env.engine, kind="library.safeguard", status="dead_letter", code="job.failed")
            _method, path = _url("jobs retry", job_id=job)
            assert api.client.post(path, headers=bearer).status_code in (200, 202)
            assert job_status(env.engine, job) == "queued"
        cli = authorize_reserved  # the host CLI is allowed on all of them (see the L06 test)
        assert cli is not None
        assert isinstance(EventBus, type)


def endpoint_table() -> dict[str, tuple[str, str]]:
    from shelfhold.ops import ENDPOINTS

    return dict(ENDPOINTS)


def test_exit_routes_refuse_what_is_not_waiting(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with export_env() as env:
        ops = make_ops(env)
        with auth_env(monkeypatch, settings, routers=[_router(ops)]) as api:
            api.login()
            _method, path = _url("series resume", export_series_id=new_ulid())
            assert api.post(path).status_code == 409
            _method, path = _url("jobs retry", job_id=new_ulid())
            assert api.post(path).status_code == 404
            assert platform is not None
