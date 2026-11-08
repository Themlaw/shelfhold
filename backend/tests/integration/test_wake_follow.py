"""EF-INT-022, EF-SRC-025, ENF-OPS-011 (Sablier): the follow cycle wakes the tool first.

A simulated Suwayomi that sleeps, a recording WakeHook (no network), a real database. The piloted
mode wakes the tool, asks it to refresh the series and only then reads the news; a wake that
times out is deferred, never counted against the source.
"""

import asyncio
from datetime import timedelta

import pytest

from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.wake import Wakes
from shelfhold.sdk.connected import ConnectedCapability
from tests.fakes_clock import ManualClock
from tests.follow_env import Bench, follow_bench, new_chapter
from tests.wake_env import RecordingHook, make_wakes

MANGA_ALPHA = 101


def _factory(hook: RecordingHook, session: timedelta = timedelta(minutes=10)):  # type: ignore[no-untyped-def]
    def build(engine, clock: ManualClock, bus: EventBus) -> Wakes:  # type: ignore[no-untyped-def]
        return make_wakes(engine, clock, hook, frozenset({"suwayomi"}), bus=bus, session=session)

    return build


def _asleep(bench: Bench, hook: RecordingHook) -> None:
    """Suwayomi has slept through a day: Sablier stopped it, and only the hook starts it."""
    bench.server.asleep = True
    hook.starts["suwayomi"] = bench.server.wake_up
    bench.clock.advance(hours=24)


@pytest.mark.req("EF-INT-022")
def test_wakehook_called_before_batch() -> None:
    hook = RecordingHook()
    with follow_bench(wakes=_factory(hook)) as bench:
        seen_at_wake: list[int] = []

        def start() -> None:
            seen_at_wake.append(len(bench.server.requests))
            bench.server.wake_up()

        hook.starts["suwayomi"] = start
        bench.server.asleep = True
        report = bench.run()
        assert hook.log == ["wake:suwayomi"]  # once for the batch, then the session is open
        assert seen_at_wake == [0]  # nothing was sent to the tool before it was woken
        assert report.discovered == 2 and not report.deferred
        assert ConnectedCapability.WAKE in bench.service.connected


@pytest.mark.req("EF-INT-022")
def test_wakehook_session_extended_during_long_job() -> None:
    hook = RecordingHook()
    with follow_bench(wakes=_factory(hook)) as bench:
        wakes = make_wakes(
            bench.lib.engine, bench.clock, hook, frozenset({"suwayomi"}),
            session=timedelta(milliseconds=60),
        )  # fmt: skip

        async def long_job() -> None:
            async with wakes.session("suwayomi"):
                await asyncio.sleep(0.2)  # the session is renewed every half of its duration

        asyncio.run(long_job())
        assert hook.log[0] == "wake:suwayomi" and hook.count("renew") >= 3
        renewed = hook.count("renew")
        asyncio.run(asyncio.sleep(0.1))
        assert hook.count("renew") == renewed  # the end of the job ends the renewals


@pytest.mark.req("EF-INT-022")
def test_wake_not_repeated_while_the_session_lasts() -> None:
    hook = RecordingHook()
    with follow_bench(wakes=_factory(hook)) as bench:
        bench.run()
        bench.clock.advance(minutes=2)
        bench.run()
        assert hook.count("wake") == 1  # the session of the first batch still holds
        bench.clock.advance(minutes=30)
        bench.run()
        assert hook.count("wake") == 2


@pytest.mark.req("EF-INT-022", "ENF-OPS-011")
def test_wake_timeout_not_counted_as_source_failure() -> None:
    hook = RecordingHook(timeout=True)
    with follow_bench(wakes=_factory(hook)) as bench:
        bench.server.asleep = True
        for _ in range(5):
            report = bench.run()
            bench.clock.advance(minutes=1)
            assert report.deferred and report.paused is None and not report.unavailable
        assert bench.server.requests == []  # the tool was never asked anything
        assert bench.rows("SELECT * FROM source_failure") == []  # L15: nothing counted
        assert "outage" not in str(bench.rows("SELECT health FROM source_config"))  # no outage
        assert bench.events("source.broken") == [] and bench.events("target.unreachable") == []
        assert bench.binding()["active"] and bench.binding()["disabled_reason"] is None
        hook.timeout = False
        hook.starts["suwayomi"] = bench.server.wake_up
        bench.clock.advance(minutes=1)
        assert bench.run().discovered == 2  # woken at last: the cycle goes through


@pytest.mark.req("EF-INT-022", "ENF-OPS-011")
def test_wake_deferred_event_after_3_batches() -> None:
    hook = RecordingHook(timeout=True)
    with follow_bench(wakes=_factory(hook)) as bench:
        bench.server.asleep = True
        for batch in (1, 2, 3, 4, 5):
            bench.run()
            bench.clock.advance(minutes=1)
            expected = 0 if batch < 3 else 1
            assert len(bench.events("wake.deferred")) == expected, batch  # once, at the third
        (event,) = bench.events("wake.deferred")
        assert event["batches"] == 3 and event["reason"] == "timeout"
        hook.timeout = False
        hook.starts["suwayomi"] = bench.server.wake_up
        bench.run()  # it woke: the episode is over
        hook.timeout = True
        bench.server.asleep = True
        bench.clock.advance(hours=1)
        for _ in range(3):
            bench.run()
            bench.clock.advance(minutes=1)
        assert len(bench.events("wake.deferred")) == 2  # a new episode announces itself again


@pytest.mark.req("EF-INT-022")
def test_wake_retried_at_most_once_per_half_minute() -> None:
    hook = RecordingHook(timeout=True)
    with follow_bench(wakes=_factory(hook)) as bench:
        bench.server.asleep = True
        bench.run()
        bench.run()  # the same minute: the hook is not asked again, and nothing is counted twice
        assert hook.count("wake") == 1


@pytest.mark.req("EF-SRC-025")
def test_piloted_refresh_wakes_and_refreshes_suwayomi_after_24h_sleep() -> None:
    hook = RecordingHook()
    with follow_bench(wakes=_factory(hook), check_mode="piloted") as bench:
        bench.run()  # the first look only learns what is there
        _asleep(bench, hook)
        bench.server.publish_upstream(new_chapter(5030, 3), MANGA_ALPHA)  # published meanwhile
        before = len(bench.server.refreshed)
        report = bench.run()
        assert hook.log[-1] == "wake:suwayomi"
        assert bench.server.refreshed[before:] == [MANGA_ALPHA]  # refreshed once, after the wake
        assert report.discovered == 1 and report.requested == 1
        assert bench.rows("SELECT state FROM download_request") == [{"state": "requested"}]
        bench.run()
        files = [p.name for p in bench.library_files()]
        assert len(files) == 1  # the chapter is seen, fetched and filed without any action
        assert bench.server.violations == []


@pytest.mark.req("EF-SRC-025")
def test_always_on_with_a_wakehook_wakes_but_sends_no_refresh() -> None:
    hook = RecordingHook()
    with follow_bench(wakes=_factory(hook)) as bench:
        _asleep(bench, hook)
        bench.run()
        assert hook.count("wake") == 1
        assert bench.server.refreshed == []
        assert not {"fetchChapters", "updateLibrary"} & {
            f for r in bench.server.requests for f in r.fields
        }


@pytest.mark.req("EF-SRC-025")
def test_piloted_refresh_failure_of_one_series_is_not_a_source_failure() -> None:
    hook = RecordingHook()
    with follow_bench(wakes=_factory(hook), check_mode="piloted") as bench:
        bench.add_binding("suwayomi:7002", "/series-beta")
        original = bench.service.request_refresh

        async def flaky(f, refs):  # type: ignore[no-untyped-def]
            if refs[0].remote_id == "/series-beta":
                raise TimeoutError("the source is slow")
            await original(f, refs)

        bench.service.request_refresh = flaky  # type: ignore[method-assign]
        bench.run()
        assert MANGA_ALPHA in bench.server.refreshed  # the other series was still refreshed
        assert bench.rows("SELECT * FROM source_failure") == []
