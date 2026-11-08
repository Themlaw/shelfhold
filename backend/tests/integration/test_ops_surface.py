"""EF-INT-023 (L36): the operating surface — `status`, `/healthz`, the CLI commands and the routes
that leave the waiting states, with the reserved ones refused to every key."""
# ruff: noqa: E501 (synthetic secrets; the scenarios read better long)

import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.target_state import load_health
from shelfhold.identity.public import EXIT_ROUTES, RESERVED_ACTIONS
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.local_socket import call
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import acquisition, export
from shelfhold.interfaces.cli import _ops as cli_ops
from shelfhold.interfaces.local_cli import IdentityDispatcher
from shelfhold.ops import endpoint_of
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
    assert queues["export.run"]["reason"] == "window.preempted"
    # the same list on the command line, in text and in JSON
    capsys.readouterr()
    assert run_cli(monkeypatch, api.settings, "status", "--json") == 0
    from_cli = json.loads(capsys.readouterr().out)
    assert {i["state"] for i in from_cli["waiting"]} == {"dead_letter", "auth_expired"}
    assert run_cli(monkeypatch, api.settings, "status") == 0
    text = capsys.readouterr().out
    assert f"shelfhold jobs retry {dead}" in text and "auth_expired" in text


@pytest.mark.req("ENF-OPS-011")
def test_healthz_exposes_paused_queues(api: AuthEnv) -> None:
    """A7: anonymous callers get the state only; a session or a key also gets the paused queues
    with their reason, and the version."""
    _seed_waiting(api.settings)
    anonymous = api.client.get("/healthz").json()
    assert anonymous == {"state": "ok"}  # no name, no version
    api.login()
    seen = api.client.get("/healthz").json()
    assert seen["state"] == "ok" and seen["version"]
    assert seen["paused_queues"] == ["export.run", "follow", "library.safeguard"]
    reasons = {p["queue"]: p["reason"] for p in seen["paused"]}
    assert reasons == {
        "follow": "auth_expired",
        "export.run": "window.preempted",
        "library.safeguard": "dead_letter",
    }


class _Probe:
    def migration_progress(self) -> None:
        return None

    def readiness(self) -> dict[str, bool]:
        return {}


def _healthz_as(env: ExportEnv, role: str | None) -> dict[str, Any]:
    """`/healthz` over the services of `env`, called by a principal of `role` (None: anonymous)."""
    from types import SimpleNamespace

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from shelfhold.interfaces.http_api.health import build_health_router
    from shelfhold.ops.health import OpsHealth

    app = FastAPI()
    app.include_router(
        build_health_router(OpsHealth(_Probe(), lambda: make_ops(env)), version="1.0.0")  # type: ignore[arg-type]
    )

    @app.middleware("http")
    async def principal(request, call_next):  # type: ignore[no-untyped-def]
        request.scope.setdefault("state", {})["principal"] = (
            SimpleNamespace(role=role) if role else None
        )
        return await call_next(request)

    body = TestClient(app).get("/healthz").json()
    assert isinstance(body, dict)
    return body


def test_healthz_reasons_are_for_administrators_and_never_carry_a_title() -> None:
    with export_env() as env:
        series = env.new_series("Dr Stone Secret Title")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.export_record).values(state="missing"))
        add_job(env.engine, kind="export.run", status="paused", code="window.preempted")
        assert series.edition_id

        admin = _healthz_as(env, "admin")
        member = _healthz_as(env, "member")
        anonymous = _healthz_as(env, None)

        assert admin["paused"] == [{"queue": "export.run", "reason": "window.preempted"}]
        assert member["paused_queues"] == ["export.run"]  # the names, for every signed-in caller
        assert "paused" not in member  # the reason is for administrators
        assert anonymous == {"state": "ok"}
        for body in (admin, member, anonymous):
            assert "Secret Title" not in json.dumps(body)  # no title, whoever asks


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
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    cli_env: ExportEnv,
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
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    cli_env: ExportEnv,
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
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    cli_env: ExportEnv,
    capsys: pytest.CaptureFixture[str],
) -> None:
    dead = add_job(
        cli_env.engine, kind="library.safeguard", status="dead_letter", code="job.failed"
    )
    done = add_job(cli_env.engine, kind="export.run", status="succeeded")
    assert run_cli(monkeypatch, settings, "jobs", "list", "--status", "dead_letter") == 0
    listed = capsys.readouterr().out
    assert dead in listed and done not in listed and "job.failed" in listed
    assert run_cli(monkeypatch, settings, "jobs", "retry", done) == 1  # it did not die
    assert job_status(cli_env.engine, done) == "succeeded"
    assert run_cli(monkeypatch, settings, "jobs", "retry", dead) == 0
    assert job_status(cli_env.engine, dead) == "queued"


def test_cli_target_reconnect_and_rotate_key(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    capsys: pytest.CaptureFixture[str],
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


@pytest.mark.req("ENF-OPS-020")
def test_meta_check_is_a_read_and_is_not_refused_while_restored_behind(
    settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.identity.public import local_auth
    from shelfhold.infrastructure.local_socket import RpcFailure

    with export_env() as env:
        ops = make_ops(env, keyring=FakeKeyring(), restore=FakeRestore())  # restored behind
        migrate(settings)
        auth = local_auth(create_db_engine(settings.database_path), settings)
        sock = tmp_path / "s.sock"
        with serving(sock, IdentityDispatcher(auth, ops=ops)):
            with pytest.raises(RpcFailure, match="restored_behind"):
                call(sock, "ops.meta_rebuild", {})  # a write: refused
            with pytest.raises(RpcFailure) as failure:
                call(sock, "ops.meta_check", {})  # a read: it reaches the service
            assert "restored_behind" not in str(failure.value)


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
                "restored_behind",
                "tombstone_key_unknown",
            }

            # restored behind: `accept-lost-keyring` is an exit of the freeze too (no export, no
            # way out of `key_unknown` otherwise), and says what it costs: the long phrase
            call(
                sock, "ops.tombstone_accept_lost_keyring",
                {"confirmation": "the keyring is lost, its tombstones become inert and purged content may come back"},
            )  # fmt: skip
            assert keyring.waived and keyring.waived[0][1] == "local_cli"
            keyring.waived.clear()
            # `secrets import` is an exit of the freeze for its /data half: it is the way out of
            # `key_unknown`, which blocks the adoption (M1b L06 review); nothing is replayed
            answer = call(sock, "ops.secrets_import", {"passphrase_file": "/p", "in_file": "/e"})
            assert isinstance(answer, dict) and answer["deferred"] is True
            assert keyring.imports == [("/p", "/e", "local_cli")]
            keyring.imports.clear()
            call(sock, "ops.restore_adopt", {"confirm_deletion": False})
            call(sock, "ops.tombstone_accept_lost_keyring", {"confirmation": "the keyring is lost"})
            assert keyring.waived == [("the keyring is lost", "local_cli")]


# -- the routes -------------------------------------------------------------------------------------


def _url(command: str, **ids: str) -> tuple[str, str]:
    method, path = endpoint_of(command)
    return method, path.format(**ids)


def _body(command: str) -> dict[str, Any]:
    return {
        "source set-secret": {"secret": "admin:new"},
        "target rotate-key": {"password": "a-synthetic-password"},
        "tombstone accept-lost-keyring": {"confirmation": "the keyring is lost"},
        "secrets import": {"passphrase_file": "p", "in_file": "export.age"},
        "reconcile --adopt-after-restore [confirm-deletion]": {"confirm_deletion": True},
        "reconcile --adopt-after-restore": {"confirm_deletion": False},
        "target recheck [child_exposure]": {},
        "rate": {"age": "13+"},
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
        ids = {"target_id": env.target_id, "work_id": env.new_series().work_id}
        reserved = [r for r in EXIT_ROUTES if r.reserved is not None]
        served = [r for r in reserved if r.command in endpoint_table()]
        assert {r.command for r in served} >= {
            "source set-secret", "target rotate-key", "tombstone accept-lost-keyring",
            "secrets import", "target recheck [child_exposure]",
            "reconcile --adopt-after-restore [confirm-deletion]",
        }  # fmt: skip
        with auth_env(monkeypatch, settings, ops_services=ops) as api:
            capsys.readouterr()
            assert (
                run_cli(monkeypatch, api.settings, "admin", "keys", "create", "--name", "ci") == 0
            )
            key = KEY.search(capsys.readouterr().out)
            assert key
            bearer = {"Authorization": f"Bearer {key.group(0)}"}
            api.login()
            for route in served:
                method, path = _url(route.command, **ids)
                refused = api.client.request(
                    method, path, json=_body(route.command), headers=bearer
                )
                assert refused.status_code == 403, route.command
                assert refused.json()["type"] == "/api/v1/problems/credential-not-allowed"
            assert keyring.waived == [] and restore.calls == []  # nothing ran
            assert keyring.imports == []
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
                if wants_fresh:
                    assert stale.status_code == 401, route.command
                    assert stale.json()["type"].endswith("reauth-required")
                else:  # no freshness asked: an old session still goes through
                    assert stale.status_code in (200, 202, 204), route.command
            # a non-reserved exit is open to an admin key
            job = add_job(
                env.engine, kind="library.safeguard", status="dead_letter", code="job.failed"
            )
            _method, path = _url("jobs retry", job_id=job)
            assert api.client.post(path, headers=bearer).status_code in (200, 202)
            assert job_status(env.engine, job) == "queued"


def endpoint_table() -> dict[str, tuple[str, str]]:
    from shelfhold.ops import ENDPOINTS

    return dict(ENDPOINTS)


def test_exit_routes_refuse_what_is_not_waiting(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with export_env() as env:
        ops = make_ops(env)
        with auth_env(monkeypatch, settings, ops_services=ops) as api:
            api.login()
            _method, path = _url("series resume", export_series_id=new_ulid())
            assert api.post(path).status_code == 409
            _method, path = _url("jobs retry", job_id=new_ulid())
            assert api.post(path).status_code == 404


# -- the two ports of the lots in flight, through the real channels ----------------------------------


def test_restored_behind_and_tombstone_key_unknown_leave_through_the_api(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The state is read on `GET /api/v1/status`, the exit route is called with a fresh session,
    and the next `status` no longer lists it; the ports saw the call with the right credential."""
    with export_env() as env:
        keyring, restore = FakeKeyring(), FakeRestore()
        ops = make_ops(env, keyring=keyring, restore=restore)

        def listed(api: AuthEnv) -> set[str]:
            return {i["state"] for i in api.client.get("/api/v1/status").json()["waiting"]}

        with auth_env(monkeypatch, settings, ops_services=ops) as api:
            api.login()
            assert {"restored_behind", "tombstone_key_unknown"} <= listed(api)
            _method, path = _url("reconcile --adopt-after-restore")
            assert api.post(path, json={"confirm_deletion": False}).status_code in (200, 202, 204)
            assert restore.calls == [False]
            assert "restored_behind" not in listed(api)
            _method, path = _url("tombstone accept-lost-keyring")
            done = api.post(path, json={"confirmation": "the keyring is lost"})
            assert done.status_code in (200, 202, 204)
            assert [c for c, _who in keyring.waived] == ["the keyring is lost"]
            assert "tombstone_key_unknown" not in listed(api)


def test_restored_behind_leaves_through_the_local_socket(
    settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.identity.public import local_auth

    with export_env() as env:
        restore = FakeRestore()
        ops = make_ops(env, restore=restore, keyring=FakeKeyring())
        migrate(settings)
        auth = local_auth(create_db_engine(settings.database_path), settings)
        sock = tmp_path / "s.sock"
        with serving(sock, IdentityDispatcher(auth, ops=ops)):
            before = call(sock, "ops.status", {})
            assert isinstance(before, dict)
            assert "restored_behind" in {i["state"] for i in before["waiting"]}
            call(sock, "ops.restore_adopt", {"confirm_deletion": True})
            assert restore.calls == [True]
            after = call(sock, "ops.status", {})
            assert isinstance(after, dict)
            assert "restored_behind" not in {i["state"] for i in after["waiting"]}


# -- the socket allow-list ---------------------------------------------------------------------------


def test_an_unknown_ops_method_is_not_found_on_the_socket(
    settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.identity.public import local_auth
    from shelfhold.infrastructure.local_socket import RpcFailure

    with export_env() as env:
        ops = make_ops(env)
        migrate(settings)
        auth = local_auth(create_db_engine(settings.database_path), settings)
        sock = tmp_path / "s.sock"
        with serving(sock, IdentityDispatcher(auth, ops=ops)):
            for name in (
                "ops.x",
                "ops.",
                "ops.__init__",
                "ops.status ",
                "ops.engine",
                "OPS.status",
            ):
                with pytest.raises(RpcFailure) as refused:
                    call(sock, name, {})
                assert refused.value.code == -32601, name  # METHOD_NOT_FOUND


def test_the_reserved_column_of_the_socket_table_matches_the_catalogue() -> None:
    from shelfhold.interfaces.local_cli.ops import OPS_METHODS
    from shelfhold.ops import M1A_STATES, UNSERVED

    m1a = {s.value for s in M1A_STATES}  # the M1b exits (rate, root, store) come with M1b
    declared = {
        r.reserved.name
        for r in EXIT_ROUTES
        if r.reserved and r.waiting_state in m1a and r.command not in UNSERVED
    }
    declared.discard("target.recheck_child_exposure")  # conditional: not a fixed property
    declared.add("purge.now")  # L41: reserved, but not the exit of a waiting state
    declared.add("intents.resolve")  # L51: reserved; the drift it settles is no waiting state
    declared.add("normalize.migrate")  # L55: reserved; no waiting state either
    declared.add("shelf.withdraw")  # M1b L10: applying the removal plan of a family shelf
    declared.add("rate")  # M1b L11: the exit of rating_pending and follow_suspended
    on_socket = {action for _impl, action in OPS_METHODS.values() if action}
    assert on_socket == declared
    for action in on_socket:  # and every name is a real action of the registry
        assert action in RESERVED_ACTIONS
