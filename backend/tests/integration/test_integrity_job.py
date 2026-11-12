"""EF-BIB-012 / ENF-OPS-014: the integrity job is a window job, bounded, and quiet outside it."""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.jobs.integrity_job import (
    INTEGRITY_KIND,
    integrity_handler,
    register_integrity_job,
)
from shelfhold.jobs.public import DEFAULT_WINDOW
from shelfhold.jobs.window import window_rank
from tests.export_env import export_env
from tests.integrity_env import Rig, flip_page_byte
from tests.l41_env import context, instance
from tests.upgrade_env import export_one

pytestmark = pytest.mark.req("EF-BIB-012")

INSIDE = datetime(2026, 11, 12, 3, 30, tzinfo=UTC)  # 04:30 in Paris, inside 03:00-07:00
NOON = datetime(2026, 11, 12, 11, 0, tzinfo=UTC)


class _Scheduler:
    def __init__(self) -> None:
        self.added: list[tuple[str, bool]] = []

    def add(self, kind: str, _cron: str, *, windowed: bool = False, **_: Any) -> None:
        self.added.append((kind, windowed))


class _Pools:
    def __init__(self) -> None:
        self.scheduler = _Scheduler()
        self.window = DEFAULT_WINDOW
        self.registered: dict[str, dict[str, Any]] = {}

    def register(self, kind: str, **options: Any) -> None:
        self.registered[kind] = options


def test_the_integrity_job_is_a_window_job_ranked_by_the_window(
    settings: Settings, tmp_path
) -> None:
    with instance(settings, tmp_path) as inst:
        pools = _Pools()
        register_integrity_job(pools, inst.comp)  # type: ignore[arg-type]
        assert pools.registered[INTEGRITY_KIND]["windowed"] is True  # never by day
        assert pools.scheduler.added == [(INTEGRITY_KIND, True)]
        assert window_rank(INTEGRITY_KIND) < window_rank("not_a_window_job")


def _comp(rig: Rig) -> Any:
    return SimpleNamespace(
        engine=rig.env.engine,
        bus=EventBus(rig.env.clock),
        clock=rig.env.clock,
        settings=SimpleNamespace(data_dir=rig.env.lib.data),
    )


def test_inside_the_window_the_batch_checks_and_asks_for_the_corrupt_unit_again() -> None:
    with export_env() as env:
        rig = Rig(env, export_one(env))
        flip_page_byte(rig.received())
        env.clock.set(INSIDE)
        ctx = context(INTEGRITY_KIND)
        integrity_handler(_comp(rig), DEFAULT_WINDOW)(ctx)
        assert (ctx.counters["corrupt"], ctx.counters["requested"]) == (1, 1)
        assert rig.state()[0] == "corrupt" and len(rig.requests()) == 1


def test_outside_the_window_no_file_is_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    from shelfhold.library.application import integrity

    def refuse(*_a: object, **_k: object) -> object:
        raise AssertionError("a file was opened outside the window")

    monkeypatch.setattr(integrity, "safe_open_archive", refuse)
    with export_env() as env:
        rig = Rig(env, export_one(env))
        env.clock.set(NOON)
        ctx = context(INTEGRITY_KIND)
        integrity_handler(_comp(rig), DEFAULT_WINDOW)(ctx)
        assert ctx.preempted is True
        assert rig.state()[0] == "unchecked"


def test_a_closing_window_stops_the_batch_between_two_units() -> None:
    with export_env() as env:
        rig = Rig(env, export_one(env))
        env.clock.set(INSIDE)
        ctx = context(INTEGRITY_KIND)
        ctx.window_end = INSIDE  # already closing
        integrity_handler(_comp(rig), DEFAULT_WINDOW)(ctx)
        assert ctx.preempted is True and rig.state()[0] == "unchecked"
