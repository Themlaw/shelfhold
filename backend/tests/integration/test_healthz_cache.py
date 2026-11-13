"""`/healthz` is cheap and constant (ENF-OPS-003): the heavy part of the status (the roots, the
restore, the keyring...) is computed by one caller at a time and kept for `HEALTH_CACHE_TTL`
seconds; what depends on the caller (the version, the reason of each queue) is never cached.

A change made by the instance (a queue paused or resumed, a restore or keyring exit, maintenance)
refreshes the shared value at once; one made from outside shows at most `HEALTH_CACHE_TTL` seconds
later, the time the shared value lives. The HTTP tests run `create_app()` as composed.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Callable, Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.ops import health as health_module
from shelfhold.ops.health import HEALTH_CACHE_TTL, OpsHealth
from shelfhold.ops.ports import PausedQueue
from shelfhold.infrastructure.db import create_db_engine
from tests.auth_env import AuthEnv, auth_env
from tests.ops_env import add_job
from tests.visibility_env import add_profile

pytestmark = pytest.mark.req("ENF-OPS-003")

RESTORE = PausedQueue("export.scan", "restore_pending", "shelfhold restore")


class _Probe:
    def migration_progress(self) -> tuple[int, int] | None:
        return None

    def readiness(self) -> dict[str, bool]:
        return {}


class _Ops:
    """The heavy status: counts its runs, can be slowed down and changed. Anything else that the
    composition asks of the operating services is a no-op."""

    def __init__(self) -> None:
        self.calls = 0
        self.delay = 0.0
        self.queues = [RESTORE]
        self._lock = threading.Lock()

    def paused_queues(self) -> list[PausedQueue]:
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return list(self.queues)

    def __getattr__(self, name: str) -> Callable[..., None]:
        return lambda *_a, **_k: None


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def ops() -> _Ops:
    return _Ops()


def _health(ops: _Ops, clock: _Clock | None = None) -> OpsHealth:
    return OpsHealth(_Probe(), lambda: ops, clock=clock)  # type: ignore[arg-type]


def _primed(ops: _Ops) -> None:
    """The startup read of the composition, awaited: the counts below start from it."""
    deadline = time.monotonic() + 10
    while ops.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(health_module, "time", SimpleNamespace(monotonic=fake))
    return fake


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops) -> Iterator[AuthEnv]:
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        _primed(ops)
        yield env


def _probe_as(env: AuthEnv, role: str) -> Any:
    if role == "admin":
        assert env.login().status_code == 200
        return env.client
    if role == "anonymous":
        return env.other_client("10.0.0.9")
    if role == "child":
        return add_profile(env, role, role, "10.0.0.2", max_age_rating=10)
    return add_profile(env, role, role, "10.0.0.3")


def test_healthz_runs_the_heavy_status_once_for_many_probes(api: AuthEnv, ops: _Ops) -> None:
    admin = _probe_as(api, "admin")
    for _ in range(40):
        assert admin.get("/healthz").status_code == 200
    assert admin.head("/healthz").status_code == 200
    assert ops.calls == 1  # the startup read, and nothing more


def test_concurrent_probes_are_one_flight(ops: _Ops) -> None:
    clock = _Clock()
    health = _health(ops, clock)
    assert [q.queue for q in health.queues()] == ["export.scan"]
    clock.now += HEALTH_CACHE_TTL + 1  # stale: the next caller recomputes, slowly
    ops.delay = 0.5
    ops.queues = []
    seen: list[list[str]] = []
    started = time.perf_counter()

    def probe() -> None:
        seen.append([q.queue for q in health.queues()])

    threads = [threading.Thread(target=probe) for _ in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert ops.calls == 2, "one recomputation for thirty concurrent probes"
    assert time.perf_counter() - started < 3.0
    assert len(seen) == 30


def test_probe_during_a_slow_refresh_is_not_held(ops: _Ops) -> None:
    clock = _Clock()
    health = _health(ops, clock)
    health.queues()
    clock.now += HEALTH_CACHE_TTL + 1
    ops.delay = 1.0
    leader = threading.Thread(target=health.queues)
    leader.start()
    time.sleep(0.2)  # the leader is inside the heavy status
    started = time.perf_counter()
    value = health.queues()
    assert time.perf_counter() - started < 0.3, "a second caller waited for the leader"
    assert [q.queue for q in value] == ["export.scan"]  # the last value
    leader.join(10)


def test_a_change_of_state_is_visible_within_the_ttl(
    clock: _Clock, api: AuthEnv, ops: _Ops
) -> None:
    admin = _probe_as(api, "admin")
    assert admin.get("/healthz").json()["paused_queues"] == ["export.scan"]
    ops.queues = []  # the restore ends, the maintenance mode is left, the keyring is unlocked
    clock.now += HEALTH_CACHE_TTL - 0.1
    assert admin.get("/healthz").json()["paused_queues"] == ["export.scan"]
    clock.now += 0.2  # past the TTL: the next probe refreshes (the last value may be served once)
    deadline = time.monotonic() + 5
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = admin.get("/healthz").json()
        if body["paused_queues"] == []:
            break
        time.sleep(0.05)
    assert body["paused_queues"] == [], "a change of state stayed invisible past the TTL"


@pytest.mark.parametrize("role", ["child", "member"])
def test_a_non_admin_never_sees_what_an_admin_sees_even_from_the_cache(
    api: AuthEnv, role: str
) -> None:
    admin = _probe_as(api, "admin").get("/healthz").json()
    assert admin["paused"] == [{"queue": "export.scan", "reason": "restore_pending"}]
    other = _probe_as(api, role).get("/healthz").json()
    assert "paused" not in other
    assert other["paused_queues"] == ["export.scan"]  # the names are for any session
    assert "shelfhold restore" not in str(other)
    assert "restore_pending" not in str(other)


def test_an_anonymous_never_sees_what_an_authenticated_caller_sees_from_the_cache(
    api: AuthEnv,
) -> None:
    _probe_as(api, "admin").get("/healthz")  # the cache is warm, for an admin
    anonymous = _probe_as(api, "anonymous")
    assert anonymous.get("/healthz").json() == {"state": "ok"}
    assert anonymous.head("/healthz").status_code == 200


def test_the_cache_holds_no_principal(ops: _Ops) -> None:
    """What is kept is the common list only, never a response or what depends on the caller."""
    health = _health(ops)
    health.queues()
    kept = {name: type(value).__name__ for name, value in vars(health).items()}
    assert "list" in kept.values() and not {"dict", "JSONResponse", "Principal"} & set(
        kept.values()
    )


def test_healthz_runs_on_the_event_loop_not_in_a_pool_thread(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[bool] = []

    def progress(_self: OpsHealth) -> None:
        try:
            asyncio.get_running_loop()
            seen.append(True)  # called from the loop: the route is a coroutine
        except RuntimeError:
            seen.append(False)  # called from a worker thread of the pool

    monkeypatch.setattr(OpsHealth, "migration_progress", progress)
    assert api.other_client("10.0.0.9").get("/healthz").status_code == 200
    assert seen == [True]


def test_a_cached_probe_takes_no_thread_of_the_pool(
    api: AuthEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the value cached, the probe needs no thread of the server's pool (the pool may be
    full of decodes): the route's own door to the pool is closed to it, and it still answers."""
    admin = _probe_as(api, "admin")
    assert admin.get("/healthz").status_code == 200  # warm

    async def refused(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("a cached /healthz asked for a thread of the pool")

    monkeypatch.setattr("shelfhold.interfaces.http_api.health.run_in_threadpool", refused)
    response = admin.get("/healthz")
    assert response.status_code == 200
    assert response.json()["paused_queues"] == ["export.scan"]


def test_prime_reads_the_status_before_the_first_probe(ops: _Ops) -> None:
    health = _health(ops)
    health.prime()
    deadline = time.monotonic() + 5
    while ops.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    assert [q.queue for q in health.queues()] == ["export.scan"]
    assert ops.calls == 1, "the first probe read the status again"


def test_close_waits_for_the_background_read_before_the_engine_goes(ops: _Ops) -> None:
    """A read of the status still running at shutdown would open a connection after the engine
    is disposed, and leave descriptors behind."""
    ops.delay = 0.5
    health = _health(ops)
    health.prime()
    health.close()
    assert ops.calls == 1
    assert not any(t.name == "health-prime" and t.is_alive() for t in threading.enumerate())


# -- a change made by the instance is visible at once ---------------------------------------------


def _queued_job(settings: Settings, kind: str) -> None:
    engine = create_db_engine(settings.database_path)
    try:
        add_job(engine, kind=kind, status="queued")
    finally:
        engine.dispose()


def test_pausing_then_resuming_a_queue_shows_on_healthz_without_waiting_for_the_ttl(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    with auth_env(monkeypatch, settings) as env:  # the real services; the cache is warm and idle
        env.login()
        _queued_job(settings, "export.scan")
        assert env.client.get("/healthz").json()["paused_queues"] == []  # cached, empty
        assert env.post("/api/v1/queue/pause", json={}).status_code == 200
        paused = env.client.get("/healthz").json()  # the very next probe: no TTL, no clock moved
        assert paused["paused_queues"] == ["export.scan"]
        assert [p["queue"] for p in paused["paused"]] == ["export.scan"]
        assert env.post("/api/v1/queue/resume", json={}).status_code == 200
        assert env.client.get("/healthz").json()["paused_queues"] == []


def test_a_probe_after_a_mutation_asks_for_no_thread_of_the_pool(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    """The refresh is paid by the mutation: the probe still answers from the cache alone."""
    with auth_env(monkeypatch, settings) as env:
        env.login()
        _queued_job(settings, "export.scan")
        assert env.post("/api/v1/queue/pause", json={}).status_code == 200

        async def refused(*_a: Any, **_k: Any) -> Any:
            raise AssertionError("/healthz asked for a thread of the pool")

        monkeypatch.setattr("shelfhold.interfaces.http_api.health.run_in_threadpool", refused)
        assert env.client.get("/healthz").json()["paused_queues"] == ["export.scan"]


def test_an_anonymous_mutation_triggers_no_refresh(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, ops: _Ops
) -> None:
    with auth_env(monkeypatch, settings, ops_services=ops) as env:
        _primed(ops)
        before = ops.calls
        anonymous = env.other_client("10.0.0.9")
        assert anonymous.post("/api/v1/queue/pause", json={}).status_code in (401, 403)
        assert ops.calls == before, "a caller with no session made the instance read its status"


def test_refresh_waits_for_a_read_in_flight_and_reads_again(ops: _Ops) -> None:
    """A read that began before the change may carry the old state: `refresh` reads once more."""
    health = _health(ops)
    health.queues()
    ops.delay = 0.3
    ops.queues = []
    lead = threading.Thread(target=health.refresh)
    lead.start()
    time.sleep(0.1)  # the first refresh is inside the status
    ops.queues = [RESTORE]  # the change lands while it reads
    health.refresh()
    lead.join(10)
    assert [q.queue for q in health.queues()] == ["export.scan"]
    assert ops.calls == 3


def test_refresh_keeps_the_last_value_when_the_status_cannot_be_read(ops: _Ops) -> None:
    health = _health(ops)
    assert [q.queue for q in health.queues()] == ["export.scan"]

    def broken() -> list[PausedQueue]:
        raise RuntimeError("boom")

    ops.paused_queues = broken  # type: ignore[method-assign]
    health.refresh()  # logged, never raised
    assert [q.queue for q in health.queues()] == ["export.scan"]
