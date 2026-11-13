"""`/healthz` is cheap and constant (ENF-OPS-003): the heavy part of the status (the roots, the
restore, the keyring...) is computed by one caller at a time and kept for `HEALTH_CACHE_TTL`
seconds; what depends on the caller (the version, the reason of each queue) is never cached.

Visibility of a change of state (maintenance, restore, keyring): at most `HEALTH_CACHE_TTL`
seconds later, the time the shared value lives.
"""

from __future__ import annotations

import inspect
import threading
import time
from types import SimpleNamespace
from typing import Any

import anyio
import anyio.to_thread
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from shelfhold.interfaces.http_api.health import build_health_router
from shelfhold.ops.health import HEALTH_CACHE_TTL, OpsHealth
from shelfhold.ops.ports import PausedQueue

pytestmark = pytest.mark.req("ENF-OPS-003")


class _Probe:
    def migration_progress(self) -> tuple[int, int] | None:
        return None

    def readiness(self) -> dict[str, bool]:
        return {}


class _Ops:
    """The heavy status: counts its runs, can be slowed down and changed."""

    def __init__(self) -> None:
        self.calls = 0
        self.delay = 0.0
        self.queues = [PausedQueue("export.scan", "restore_pending", "shelfhold restore")]
        self._lock = threading.Lock()

    def paused_queues(self) -> list[PausedQueue]:
        with self._lock:
            self.calls += 1
        time.sleep(self.delay)
        return list(self.queues)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def _health(ops: _Ops, clock: _Clock | None = None) -> OpsHealth:
    return OpsHealth(_Probe(), lambda: ops, clock=clock or time.monotonic)  # type: ignore[arg-type]


def _client(health: OpsHealth) -> TestClient:
    app = FastAPI()

    @app.middleware("http")
    async def principal(request: Request, call_next: Any) -> Any:
        role = request.headers.get("x-role")
        if role:
            request.scope["state"] = {"principal": SimpleNamespace(role=role)}
        return await call_next(request)

    app.include_router(build_health_router(health, version="9.9"))
    return TestClient(app)


@pytest.fixture
def ops() -> _Ops:
    return _Ops()


def test_healthz_runs_the_heavy_status_once_for_many_probes(ops: _Ops) -> None:
    client = _client(_health(ops))
    for _ in range(40):
        assert client.get("/healthz", headers={"x-role": "admin"}).status_code == 200
    assert ops.calls == 1


def test_healthz_head_and_get_share_the_value(ops: _Ops) -> None:
    client = _client(_health(ops))
    client.get("/healthz", headers={"x-role": "admin"})
    client.head("/healthz", headers={"x-role": "admin"})
    client.get("/healthz", headers={"x-role": "member"})
    assert ops.calls == 1


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


def test_a_change_of_state_is_visible_within_the_ttl(ops: _Ops) -> None:
    clock = _Clock()
    client = _client(_health(ops, clock))
    admin = {"x-role": "admin"}
    assert client.get("/healthz", headers=admin).json()["paused_queues"] == ["export.scan"]
    ops.queues = []  # the restore ends, the maintenance mode is left, the keyring is unlocked
    clock.now += HEALTH_CACHE_TTL - 0.1
    assert client.get("/healthz", headers=admin).json()["paused_queues"] == ["export.scan"]
    clock.now += 0.2  # past the TTL: the next probe refreshes (the last value may be served once)
    deadline = time.monotonic() + 5
    body: dict[str, Any] = {}
    while time.monotonic() < deadline:
        body = client.get("/healthz", headers=admin).json()
        if body["paused_queues"] == []:
            break
        time.sleep(0.05)
    assert body["paused_queues"] == [], "a change of state stayed invisible past the TTL"


@pytest.mark.parametrize("role", ["child", "member"])
def test_a_non_admin_never_sees_what_an_admin_sees_even_from_the_cache(
    ops: _Ops, role: str
) -> None:
    client = _client(_health(ops))
    admin = client.get("/healthz", headers={"x-role": "admin"}).json()
    assert admin["paused"] == [{"queue": "export.scan", "reason": "restore_pending"}]
    other = client.get("/healthz", headers={"x-role": role}).json()
    assert "paused" not in other
    assert "shelfhold restore" not in str(other)
    assert "restore_pending" not in str(other)


def test_an_anonymous_never_sees_what_an_authenticated_caller_sees_from_the_cache(
    ops: _Ops,
) -> None:
    client = _client(_health(ops))
    client.get("/healthz", headers={"x-role": "admin"})
    assert client.get("/healthz").json() == {"state": "ok"}
    assert client.head("/healthz").status_code == 200


def test_the_cache_holds_no_principal(ops: _Ops) -> None:
    """What is kept is the common part only: nothing of the response is keyed or filled by who
    asked first."""
    health = _health(ops)
    client = _client(health)
    client.get("/healthz", headers={"x-role": "admin"})
    kept = repr(vars(health))
    assert "principal" not in kept and "admin" not in kept and "9.9" not in kept


def test_healthz_is_a_coroutine_that_does_not_take_a_pool_thread(ops: _Ops) -> None:
    router = build_health_router(_health(ops))
    route = next(r for r in router.routes if getattr(r, "path", "") == "/healthz")
    assert inspect.iscoroutinefunction(route.endpoint)  # type: ignore[attr-defined]


def test_a_cached_probe_takes_no_thread_of_the_pool(
    ops: _Ops, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the value cached, a probe needs no thread of the server's pool (the pool may be
    full of decodes): the pool is closed to it, and it still answers."""
    health = _health(ops)
    client = _client(health)
    assert client.get("/healthz", headers={"x-role": "admin"}).status_code == 200  # warm

    async def refused(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a cached /healthz asked for a thread of the pool")

    monkeypatch.setattr(anyio.to_thread, "run_sync", refused)
    response = client.get("/healthz", headers={"x-role": "admin"})
    assert response.status_code == 200
    assert response.json()["paused_queues"] == ["export.scan"]


def test_prime_reads_the_status_before_the_first_probe(ops: _Ops) -> None:
    health = _health(ops)
    health.prime()
    deadline = time.monotonic() + 5
    while ops.calls == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.05)
    client = _client(health)
    assert client.get("/healthz", headers={"x-role": "admin"}).status_code == 200
    assert ops.calls == 1, "the first probe read the status again"
