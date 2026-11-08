"""EF-SRC-026 (a, b, c, d), ENF-OPS-011 (M1b L22): the functional probe and the bounded requeue.

A simulated Suwayomi, a real database, no network. The probe only reads; the requeue only touches
the chapters Shelfhold itself asked for (`download_request`), never while the server's queue holds
anything, never for a tombstone, a paused or an unloaded source, and never above the cap.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.library.public import VariantRef
from tests.fakes.suwayomi import FakeSuwayomi
from tests.follow_env import Bench, follow_bench
from tests.wake_env import RecordingHook, make_wakes

READ_ONLY = frozenset({"aboutServer", "settings", "sources", "mangas", "manga", "chapters",
                       "chapter", "categories", "downloadStatus"})  # fmt: skip
SOURCE_ALPHA = "7001"


def _drop_source(server: FakeSuwayomi, source_id: str) -> dict[str, Any]:
    nodes = server.state["sources"]
    gone = next(n for n in nodes if n["id"] == source_id)
    nodes.remove(gone)
    return gone


def _requested(bench: Bench, *numbers: int) -> None:
    """Chapters appear upstream and the cycle asks for them; the server then drops the downloads
    (its queue is empty again, and the chapters never arrive)."""
    for number in numbers:
        bench.add_chapter(number)
    bench.run()
    bench.server._enqueued.clear()  # type: ignore[attr-defined]


def _stuck(bench: Bench, monkeypatch: pytest.MonkeyPatch) -> None:
    async def poll(f: Any, t: Any) -> str:
        return "downloading"

    monkeypatch.setattr(bench.service, "poll", poll)


def _enqueued_ids(bench: Bench) -> list[int]:
    return [
        int(r.json()["variables"]["id"])
        for r in bench.server.requests
        if "enqueueChapterDownload" in r.fields
    ]


def _attempts(bench: Bench) -> list[int]:
    return [r["attempts"] for r in bench.rows("SELECT attempts FROM download_request ORDER BY id")]


# -- (a) the functional probe ----------------------------------------------------------------------


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_semantic_health_detects_unloaded_source() -> None:
    with follow_bench() as bench:
        _drop_source(bench.server, SOURCE_ALPHA)  # the extension is gone
        report = bench.run()
        events = bench.events("source.not_loaded")
        assert [e["source_key"] for e in events] == ["suwayomi:7001"]
        assert any(e.type == "source.not_loaded" for e in bench.delivered)
        assert bench.rows("SELECT * FROM source_failure") == []  # never a content error (L15)
        assert bench.rows("SELECT * FROM download_request") == [] and report.paused is None
        assert bench.run().discovered == 0 and len(bench.events("source.not_loaded")) == 1  # once
        assert all(r.fields <= READ_ONLY for r in bench.server.requests)  # the probe only reads


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
@pytest.mark.parametrize("damage", ["stub", "half_updated"])
def test_semantic_health_detects_stub_and_half_updated_sources(damage: str) -> None:
    with follow_bench() as bench:
        node = next(n for n in bench.server.state["sources"] if n["id"] == SOURCE_ALPHA)
        if damage == "stub":
            node["displayName"] = "Source not installed: 7001"  # what the server puts instead
        else:
            node.pop("lang")  # an extension caught between two versions: an incomplete record
        bench.run()
        assert [e["source_key"] for e in bench.events("source.not_loaded")] == ["suwayomi:7001"]
        assert bench.rows("SELECT * FROM source_failure") == []


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_unloaded_source_is_probed_after_the_wake_and_resumes_when_loaded() -> None:
    hook = RecordingHook()

    def build(engine: Any, clock: Any, bus: Any) -> Any:
        return make_wakes(engine, clock, hook, frozenset({"suwayomi"}), bus=bus)

    with follow_bench(wakes=build) as bench:
        gone = _drop_source(bench.server, SOURCE_ALPHA)
        bench.server.asleep = True
        hook.starts["suwayomi"] = bench.server.wake_up
        bench.run()
        assert hook.log[0] == "wake:suwayomi"  # the probe goes through the L21 wake
        assert len(bench.events("source.not_loaded")) == 1
        bench.server.state["sources"].append(gone)  # the extension is back
        assert bench.run().discovered == 2  # followed again, nothing more announced
        assert len(bench.events("source.not_loaded")) == 1
        bench.server.state["sources"].remove(gone)  # lost again: a new episode, told again
        bench.run()
        assert len(bench.events("source.not_loaded")) == 2


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_abandoned_chapters_reopen_when_the_source_is_loaded_again() -> None:
    with follow_bench(max_attempts=2) as bench:
        bench.run()
        _requested(bench, 3)
        bench.rows("SELECT 1")
        with bench.lib.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE download_request SET state = 'abandoned', attempts = 2"
            )
        gone = _drop_source(bench.server, SOURCE_ALPHA)
        bench.run()
        assert bench.rows("SELECT state FROM download_request")[0]["state"] == "abandoned"
        bench.server.state["sources"].append(gone)
        bench.run()
        assert bench.rows("SELECT state FROM download_request")[0]["state"] != "abandoned"


# -- (b) the bounded requeue -------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_requeue_limited_to_shelfhold_requested_chapters(monkeypatch: pytest.MonkeyPatch) -> None:
    with follow_bench() as bench:
        bench.run()  # Alpha's two chapters are the backlog: never asked, never retried
        _requested(bench, 3)
        _stuck(bench, monkeypatch)
        asked = _enqueued_ids(bench)
        assert len(asked) == 1
        bench.clock.advance(hours=7)
        bench.run()
        assert _attempts(bench) == [1]  # the one request of Shelfhold was asked again
        bench.run()
        assert _enqueued_ids(bench) == asked * 2  # and nothing else, ever
        assert len(bench.rows("SELECT * FROM download_request")) == 1


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_requeue_waits_while_the_server_queue_is_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    with follow_bench() as bench:
        bench.run()
        _requested(bench, 3)
        _stuck(bench, monkeypatch)
        bench.server._enqueued.add(9999)  # type: ignore[attr-defined]  # another client's work
        bench.clock.advance(hours=7)
        bench.run()
        assert _attempts(bench) == [0] and bench.events("source.downloads_dropped") == []
        bench.server._enqueued.clear()  # type: ignore[attr-defined]
        bench.run()
        assert _attempts(bench) == [1]


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_requeue_threshold_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    with follow_bench(stall_after=timedelta(hours=1)) as bench:
        bench.run()
        _requested(bench, 3)
        _stuck(bench, monkeypatch)
        bench.clock.advance(minutes=50)
        bench.run()
        assert _attempts(bench) == [0]
        bench.clock.advance(minutes=20)
        bench.run()
        assert _attempts(bench) == [1]


class _Gate:
    """A tombstone gate that refuses the variants whose remote id it was told."""

    def __init__(self) -> None:
        self.refused: set[str] = set()

    def check(self, candidate: object, *, root_id: str | None = None, **_: Any) -> Any:
        hit = isinstance(candidate, VariantRef) and any(
            candidate.remote_unit_id.endswith(r) for r in self.refused
        )
        return SimpleNamespace(blocked=hit)

    def check_content(self, hashes: object = (), **_: Any) -> bool:
        return False


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011", "EF-BIB-053")
def test_requeue_skips_tombstoned_chapter_and_paused_or_unloaded_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _Gate()
    with follow_bench(tombstones=lambda _lib: gate) as bench:
        bench.run()
        _requested(bench, 3)
        _stuck(bench, monkeypatch)
        bench.clock.advance(hours=7)
        gate.refused.add("ch-003")  # purged for good meanwhile
        bench.run()
        assert _attempts(bench) == [0]
        gate.refused.clear()
        with bench.lib.engine.begin() as connection:  # the service waits for an admin
            connection.exec_driver_sql(
                "UPDATE source_config SET health = json_set(coalesce(health, '{}'), '$.pause', "
                "json('{\"failure_class\": \"instance\", \"event_type\": "
                "\"source.instance_changed\", \"waiting_state\": \"source_instance_changed\"}'))"
            )
        bench.run()
        assert _attempts(bench) == [0]
        with bench.lib.engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE source_config SET health = json_remove(health, '$.pause')"
            )
        gone = _drop_source(bench.server, SOURCE_ALPHA)  # not loaded
        bench.run()
        assert _attempts(bench) == [0]
        bench.server.state["sources"].append(gone)
        bench.run()
        assert _attempts(bench) == [1]


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_dropped_chapters_requeued_with_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    with follow_bench(requeue_cap=2) as bench:
        bench.run()
        _requested(bench, 3, 4, 5)
        _stuck(bench, monkeypatch)
        bench.clock.advance(hours=7)
        report = bench.run()
        assert sorted(_attempts(bench)) == [0, 1, 1] and report.tickets.dropped == 2
        assert [e["count"] for e in bench.events("source.downloads_dropped")] == [2]
        assert any(e.type == "source.downloads_dropped" for e in bench.delivered)
        bench.run()  # the third waits for the next cycle; the first two are young again
        assert sorted(_attempts(bench)) == [1, 1, 1]
        assert [e["count"] for e in bench.events("source.downloads_dropped")] == [2, 1]


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_requeue_is_idempotent_under_replay_and_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with follow_bench() as bench:
        bench.run()
        _requested(bench, 3)
        _stuck(bench, monkeypatch)
        bench.clock.advance(hours=7)
        tickets = bench.cycle._tickets
        seen = tickets._open_requests()  # two workers read the same row...
        assert len(seen) == 1 and seen[0].ticket is not None
        report = tickets_report()
        first = tickets._requeue(seen[0], report)
        second = tickets._requeue(seen[0], report)  # ...and both want to send it again
        assert (first, second) == (True, False) and _attempts(bench) == [1]
        assert report.dropped == 1
        before = len(_enqueued_ids(bench))
        for _ in range(2):  # a replay of the cycle: one new send, then the clock starts again
            bench.run()
        assert len(_enqueued_ids(bench)) == before + 1 and _attempts(bench) == [1]


def tickets_report() -> Any:
    from shelfhold.acquisition.public import TicketReport

    return TicketReport()


# -- (c) the aggregated freshness event --------------------------------------------------------------


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_series_stale_aggregated() -> None:
    with follow_bench() as bench:
        beta = bench.add_binding("suwayomi:7002", "/series-beta")
        beta_edition = bench.rows(
            "SELECT edition_id FROM source_binding WHERE id = :b", b=beta
        )[0]["edition_id"]
        bench.run()
        bench.clock.advance(days=15)  # both are past the 14-day floor
        found = bench.cycle.check_freshness()
        assert sorted(found) == sorted([bench.edition_id, beta_edition])
        events = bench.events("series.stale")
        assert len(events) == 1  # one event for the cycle, not one per series
        assert events[0]["count"] == 2
        assert sorted(events[0]["edition_ids"]) == sorted([bench.edition_id, beta_edition])
        assert bench.cycle.check_freshness() == [] and len(bench.events("series.stale")) == 1
        bench.add_chapter(3)  # Alpha wakes up, then goes silent again; Beta stays announced
        bench.run()
        bench.clock.advance(days=22)
        assert bench.cycle.check_freshness() == [bench.edition_id]
        assert [e["count"] for e in bench.events("series.stale")] == [2, 1]


@pytest.mark.req("EF-SRC-026", "ENF-OPS-011")
def test_a_cycle_with_stale_series_emits_a_single_event() -> None:
    with follow_bench() as bench:
        bench.add_binding("suwayomi:7002", "/series-beta")
        bench.run()
        bench.clock.advance(days=15)
        bench.run()  # the cycle itself runs the freshness check
        assert len(bench.events("series.stale")) == 1 and asyncio.iscoroutinefunction(bench.cycle.run)


# -- (d) the global update filters ---------------------------------------------------------------------


@pytest.mark.req("EF-SRC-026")
@pytest.mark.parametrize("mode", ["none", "ui_login"])
def test_global_update_filters_block_wizard(mode: str, settings: Any) -> None:
    from shelfhold.headless.errors import SourceUpdateFiltersBlocking
    from shelfhold.plugin_host.adapters.meta.suwayomi.probe import HttpSuwayomiProbe
    from tests.fakes.suwayomi.server import PASSWORD, USER, FakeSuwayomiFetcher
    from tests.headless_env import SUWAYOMI_URL, headless

    server = FakeSuwayomi(auth_mode=mode)
    server.expose_roles = True
    server.exclude_unread = True
    server.exclude_not_started = True
    probe = HttpSuwayomiProbe(lambda _url: FakeSuwayomiFetcher(server, SUWAYOMI_URL))  # type: ignore[arg-type,return-value]
    secret = f"{USER}:{PASSWORD}" if mode == "ui_login" else None
    with headless(settings, suwayomi=probe) as env:
        with pytest.raises(SourceUpdateFiltersBlocking) as refused:
            env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=secret)
        message = str(refused.value)
        assert "excludeUnreadChapters" in message and "excludeNotStarted" in message
        assert "désactiv" in message  # the correction proposed
        assert env.count("source_config") == 0  # nothing was stored
        assert all(r.fields <= {"aboutServer", "settings", "user", "login"} for r in server.requests)
        server.exclude_unread = server.exclude_not_started = False  # corrected on the server
        env.admin.source_add_suwayomi(url=SUWAYOMI_URL, secret=secret)
        assert env.count("source_config") == 1


@pytest.mark.req("EF-SRC-026")
def test_suwayomi_adapter_reads_the_global_update_filters() -> None:
    from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
    from tests.fakes.suwayomi import BASE_URL, FakeSuwayomiFetcher

    server = FakeSuwayomi(auth_mode="none")
    service = SuwayomiService(BASE_URL)
    fetcher: Any = FakeSuwayomiFetcher(server)
    assert asyncio.run(service.blocking_update_filters(fetcher)) == []
    server.exclude_not_started = True
    server.exclude_unread = True
    assert asyncio.run(service.blocking_update_filters(fetcher)) == [
        "excludeNotStarted",
        "excludeUnreadChapters",
    ]
