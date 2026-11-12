"""M3 L13, EF-INT-018: the maintenance mode freezes every write, survives a restart, is entered and
left by a reserved action that is fresh and audited, and lets reading and `/healthz` through.

Each guard has a test that tries to get round it."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.identity.public import EXIT_ROUTES
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity, platform
from shelfhold.interfaces.local_cli.ops import (
    MAINTENANCE_OPEN,
    OPS_METHODS,
    READS,
    run_ops_method,
)
from shelfhold.jobs.huey_app import JobContext, JobPools, boot_jobs
from shelfhold.jobs.mode import PARK_CODE
from shelfhold.ops import OpsServices
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.status import collect
from tests.auth_env import AuthEnv, auth_env
from tests.fakes_clock import ManualClock
from tests.maintenance_env import Env, env

pytestmark = pytest.mark.req("EF-INT-018")

ACQUIRE = "acquire.run"
EXPORT = "export.run"


class Lock:
    def is_held(self) -> bool:
        return True


def watch(e: Env, ran: list[str], *kinds: str, writes: bool = True) -> None:
    for kind in kinds:
        e.pools.register(
            kind,
            pool="io",
            handler=lambda c, k=kind: ran.append(f"{k}:{c.dedupe_key}"),
            writes=writes,
        )


def statuses(e: Env) -> dict[str, tuple[str, str | None]]:
    return {r.dedupe_key: (r.status, r.error_code) for r in e.rows(platform.job_view)}


def test_maintenance_mode_freezes_acquisitions_and_exports(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        watch(e, ran, ACQUIRE, EXPORT)
        e.pools.submit(ACQUIRE, dedupe_key="before-entry")  # already in the queue
        e.pools.scheduler.add(EXPORT, "*/5 * * * *")  # a periodic that will fall due
        e.ops.enter(principal=e.principal())
        e.pools.submit(EXPORT, dedupe_key="after-entry")  # a job that arrives during the mode
        e.clock.advance(minutes=10)
        e.pools.tick()  # the scan claims nothing: the periodic waits
        e.run()
        assert ran == []  # nothing started, queued before or after the entry
        assert statuses(e) == {
            "before-entry": ("paused", PARK_CODE),
            "after-entry": ("paused", PARK_CODE),
        }  # parked, not failed, no attempt spent
        assert all(r.attempts == 0 for r in e.rows(platform.job_view))
        (due,) = e.rows(platform.schedule)
        assert due.next_due_at <= e.clock.now() and due.last_run_at is None  # still owed
        # the exit puts everything back, and the periodic is caught up once
        e.ops.leave(principal=e.principal())
        e.pools.tick()
        e.run()
        e.pools.tick()
        e.run()
        assert sorted(ran)[:2] == [f"{ACQUIRE}:before-entry", f"{EXPORT}:after-entry"]
        assert sum(1 for r in ran if r.startswith(f"{EXPORT}:periodic")) == 1  # once, not 2


def test_maintenance_blocks_jobs_that_write_nothing_to_the_disks_too(tmp_path: Path) -> None:
    """A `writes=False` job (a notification, a backup, a key check) still writes the database or
    sends something: the mode parks it as well."""
    with env(tmp_path) as e:
        ran: list[str] = []
        watch(e, ran, "notify.send", writes=False)
        e.ops.enter(principal=e.principal())
        e.pools.submit("notify.send", dedupe_key="n1")
        e.run()
        assert ran == [] and statuses(e)["n1"] == ("paused", PARK_CODE)


def test_a_job_in_flight_finishes_and_nothing_else_starts(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        watch(e, ran, EXPORT)

        def entering(context: JobContext) -> None:
            ran.append("running")
            assert e.ops.enter(principal=e.principal())["running_jobs"] == 1  # it is counted
            ran.append("finished")

        e.pools.register("slow", pool="io", handler=entering)
        e.pools.submit("slow", dedupe_key="s")
        e.pools.submit(EXPORT, dedupe_key="e")
        e.run()
        assert ran == ["running", "finished"]  # the running job was left to end its work
        assert statuses(e)["s"][0] == "succeeded"
        assert statuses(e)["e"] == ("paused", PARK_CODE)


def test_the_mode_survives_a_restart(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        watch(e, ran, ACQUIRE)
        e.pools.submit(ACQUIRE, dedupe_key="queued-then-restart")
        e.ops.enter(principal=e.principal())
    # a new process: new engine, new pools, nothing in memory
    engine = create_db_engine(tmp_path / "shelfhold.db")
    clock = ManualClock(e.clock.now())
    pools = JobPools(tmp_path / "data", engine=engine, clock=clock, seed="l13")
    pools.register(ACQUIRE, pool="io", handler=lambda c: ran.append(c.dedupe_key))
    try:
        assert pools.mode.active()
        boot_jobs(pools, instance_lock=Lock(), resolve_intents=lambda: None)
        pools.scheduler.add(ACQUIRE, "*/5 * * * *")
        clock.advance(minutes=30)
        pools.tick()
        huey = pools.huey("io")
        while (task := huey.dequeue()) is not None:
            huey.execute(task)
        assert ran == []  # still frozen after the restart
        with engine.connect() as conn:
            assert collect(conn)[0].state == "maintenance"
    finally:
        engine.dispose()


def test_a_database_that_cannot_be_read_counts_as_frozen(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        with e.engine.begin() as conn:
            conn.execute(sa.text("DROP TABLE instance"))
        assert e.pools.mode.active() is True  # the safe side: a write could not succeed either


def test_status_lists_maintenance_with_its_exit_command(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        with e.engine.connect() as conn:
            assert collect(conn) == []
        e.ops.enter(principal=e.principal())
        with e.engine.connect() as conn:
            (item,) = collect(conn)
        assert (item.state, item.command, item.reserved) == (
            "maintenance",
            "shelfhold maintenance off",
            True,
        )
        assert any(r.waiting_state == "maintenance" for r in EXIT_ROUTES)
        listed = e.ops.status()
        assert listed["maintenance"]["active"] is True
        e.ops.leave(principal=e.principal())
        with e.engine.connect() as conn:
            assert collect(conn) == []


# -- entering and leaving are reserved, fresh and audited ----------------------------------------


@pytest.mark.parametrize("kind", ["key", "device", "member", "child", "stale"])
@pytest.mark.parametrize("action", ["enter", "leave"])
def test_entering_or_leaving_is_refused_to_everyone_but_a_fresh_admin_or_the_host(
    tmp_path: Path, kind: str, action: str
) -> None:
    with env(tmp_path) as e:
        if action == "leave":
            e.ops.enter(principal=e.principal("cli"))
        before = e.ops.mode.state().active
        with pytest.raises(CodedRefusal):
            getattr(e.ops, action)(principal=e.principal(kind))
        assert e.ops.mode.state().active is before  # nothing moved
        audited = [r.action for r in e.rows(identity.audit)]
        assert audited == (
            ["maintenance.enter"] if action == "leave" else []
        )  # no line for a refusal


@pytest.mark.parametrize("kind", ["admin", "cli"])
def test_a_fresh_admin_session_and_the_host_may_enter_and_leave_with_an_audit_line(
    tmp_path: Path, kind: str
) -> None:
    with env(tmp_path) as e:
        who = e.principal(kind)
        assert e.ops.enter(principal=who) == {"active": True, "changed": True, "running_jobs": 0}
        assert e.ops.enter(principal=who)["changed"] is False  # again: nothing, and no second line
        assert e.ops.leave(principal=who) == {"active": False, "changed": True}
        assert e.ops.leave(principal=who)["changed"] is False
        rows = e.rows(identity.audit)
        assert [r.action for r in rows] == ["maintenance.enter", "maintenance.exit"]
        assert all(r.actor_id == who.actor_id and r.target == "instance" for r in rows)


def test_a_session_that_goes_stale_cannot_leave_the_mode(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        session = e.principal()
        e.ops.enter(principal=session)
        e.clock.advance(minutes=16)  # past the 900 s of freshness
        with pytest.raises(CodedRefusal):
            e.ops.leave(principal=session)
        assert e.ops.mode.active()


def test_the_entry_keeps_its_first_date(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        e.ops.enter(principal=e.principal())
        first = e.ops.mode.state().since
        e.clock.advance(hours=1)
        e.ops.enter(principal=e.principal())
        assert e.ops.mode.state().since == first


# -- the operating commands of the host CLI --------------------------------------------------------


def test_every_operating_method_that_is_not_a_read_is_refused_while_the_mode_is_on(
    tmp_path: Path,
) -> None:
    with env(tmp_path) as e:
        ops = OpsServices(e.engine, clock=e.clock, bus=EventBus(e.clock), maintenance=e.ops)
        e.ops.enter(principal=e.principal("cli"))
        cli = e.principal("cli")
        refused = 0
        for method in OPS_METHODS:
            if method in MAINTENANCE_OPEN:
                continue
            with pytest.raises(Exception) as caught:
                run_ops_method(ops, cli, method, {})
            assert getattr(caught.value, "data", {}).get("code") == "maintenance.active", method
            refused += 1
        assert refused == len(set(OPS_METHODS) - MAINTENANCE_OPEN) > 40
        # what stays open is reading and the exit, and nothing else
        assert READS | {"ops.maintenance_off"} == MAINTENANCE_OPEN
        assert "ops.maintenance_on" not in MAINTENANCE_OPEN
        run_ops_method(ops, cli, "ops.maintenance_off", {})
        assert not e.ops.mode.active()


def test_a_method_added_tomorrow_is_refused_by_default() -> None:
    assert "ops.some_new_write" not in MAINTENANCE_OPEN
    assert all(m.startswith("ops.") for m in MAINTENANCE_OPEN)
    assert set(OPS_METHODS) >= MAINTENANCE_OPEN


# -- HTTP: `create_app()` as composed, the flag in the real database -------------------------------


class _Ops:
    """The services behind the routes of the mode, which the tests of the service cover; what is
    played here is the gate in front of the routes."""

    def __init__(self) -> None:
        self.left = 0
        self.maintenance = SimpleNamespace(
            leave=self._leave, status=self._status, enter=self._leave
        )

    def _leave(self, **_: object) -> dict[str, object]:
        self.left += 1
        return {"active": False, "changed": True}

    @staticmethod
    def paused_queues() -> list[object]:
        return []

    def __getattr__(self, name: str) -> Callable[..., None]:  # any other service: does nothing
        return lambda *_a, **_k: None

    @staticmethod
    def _status() -> dict[str, object]:
        span = {
            "start_utc": "2026-11-13T02:00:00Z", "end_utc": "2026-11-13T06:00:00Z",
            "start_local": "2026-11-13T03:00:00+01:00", "end_local": "2026-11-13T07:00:00+01:00",
            "duration_s": 14400, "open": False,
        }  # fmt: skip
        return {
            "maintenance": {"active": True, "since": None, "running_jobs": 0},
            "queue": {"paused": [], "parked_jobs": 0},
            "window": {
                "timezone": "Europe/Paris", "start": "03:00", "end": "07:00",
                "crosses_midnight": False, "current": None, "next": span, "upcoming": [span],
            },
        }  # fmt: skip


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[tuple[AuthEnv, _Ops]]:
    ops = _Ops()
    with auth_env(monkeypatch, settings, ops_services=ops) as live:
        live.login()
        yield live, ops


def set_mode(api: AuthEnv, on: bool) -> None:
    engine = create_db_engine(api.settings.database_path)
    try:
        with engine.begin() as conn:
            if conn.execute(sa.select(platform.instance.c.instance_id)).first() is None:
                conn.execute(
                    sa.insert(platform.instance).values(instance_id=new_ulid(), jurisdiction="FR")
                )
            conn.execute(
                sa.update(platform.instance).values(
                    maintenance_since=datetime(2026, 11, 12, tzinfo=UTC) if on else None
                )
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
def test_http_writes_are_refused_with_a_503_and_the_catalog_code(
    api: tuple[AuthEnv, _Ops], method: str
) -> None:
    live, _ops = api
    set_mode(live, True)
    refused = live.client.request(
        method, "/api/v1/nope", headers={"X-CSRF-Token": live.csrf()}
    )  # a route that does not exist: the gate is in front of the routing
    assert refused.status_code == 503
    assert refused.json()["type"] == "/api/v1/problems/maintenance.active"
    assert refused.headers["retry-after"] == "60"
    assert refused.headers["x-request-id"]  # inside the request context, headers included
    assert refused.headers["x-robots-tag"] == "noindex, nofollow"  # and the security headers
    set_mode(live, False)
    assert live.client.request(method, "/api/v1/nope").status_code != 503  # off: no gate


def test_http_reserved_routes_are_refused_too(api: tuple[AuthEnv, _Ops]) -> None:
    """A fresh admin session, the very one that may do the reserved action, is refused all the
    same: the mode comes before the route."""
    live, _ops = api
    set_mode(live, True)
    for path in ("/api/v1/secrets/import", "/api/v1/maintenance/enter", "/api/v1/store/reinit"):
        refused = live.post(path, json={})
        assert refused.status_code == 503, path
        assert refused.json()["type"] == "/api/v1/problems/maintenance.active", path


def test_http_reading_and_healthz_go_through(api: tuple[AuthEnv, _Ops]) -> None:
    live, _ops = api
    set_mode(live, True)
    assert live.client.get("/api/v1/maintenance").status_code == 200  # the admin reads the mode
    assert live.client.get("/healthz").status_code == 200
    assert live.client.head("/healthz").status_code == 200
    assert live.client.get("/readyz").status_code == 200
    assert live.client.options("/api/v1/maintenance").status_code != 503


def test_http_exit_and_sign_in_stay_open_and_nothing_else(api: tuple[AuthEnv, _Ops]) -> None:
    live, ops = api
    set_mode(live, True)
    assert live.login().status_code == 200  # a session fresh enough to leave is one sign-in away
    assert live.post("/api/v1/maintenance/exit", json={}).status_code == 200
    assert ops.left == 1
    # a trailing slash, a case change or a double slash does not make a closed path an open one
    for path in (
        "/api/v1/maintenance/exit/", "/API/V1/MAINTENANCE/EXIT", "/api/v1//maintenance/exit",
    ):  # fmt: skip
        answer = live.post(path, json={})
        assert answer.status_code in (404, 405, 503), path
    assert ops.left == 1


def test_http_the_flag_is_read_at_every_request(api: tuple[AuthEnv, _Ops]) -> None:
    live, _ops = api
    for on, expected in ((False, 200), (True, 503), (False, 200)):
        set_mode(live, on)
        got = live.post("/api/v1/maintenance/enter", json={}).status_code
        assert (got == 503) is (expected == 503), on


# -- the other methods of the socket ---------------------------------------------------------------


def test_the_socket_refuses_every_config_and_identity_write_while_the_mode_is_on(
    tmp_path: Path,
) -> None:
    """`config set`, `target add`, `source add`, `shelf create`, `admin create-key`...: the two
    tables of the socket other than `ops.*` are writes too, and only their reads stay."""
    import asyncio

    from shelfhold.infrastructure.local_socket import Peer, RpcError
    from shelfhold.interfaces.local_cli.dispatch import (
        HEADLESS_METHODS,
        MAINTENANCE_OPEN,
        METHODS,
        IdentityDispatcher,
    )

    with env(tmp_path) as e:
        ops = OpsServices(e.engine, clock=e.clock, bus=EventBus(e.clock), maintenance=e.ops)
        dispatcher = IdentityDispatcher(None, headless=object(), ops=ops)  # type: ignore[arg-type]
        peer = Peer(1, 1000, 1000)
        e.ops.enter(principal=e.principal("cli"))
        writes = (set(METHODS) | set(HEADLESS_METHODS)) - MAINTENANCE_OPEN
        assert len(writes) >= 10
        for method in sorted(writes):
            with pytest.raises(RpcError) as refused:
                asyncio.run(dispatcher(peer, method, {}))
            assert (refused.value.data or {}).get("code") == "maintenance.active", method
        assert set(METHODS) | set(HEADLESS_METHODS) >= MAINTENANCE_OPEN
