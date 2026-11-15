"""The `progress.sync` job (M4 L14b): background class, parked by the maintenance mode, and an
outage of Kavita is a pause, never a failure."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.jobs.kinds import traits_of
from shelfhold.jobs.scheduler import priority_class_of
from shelfhold.runtime_jobs import PROGRESS_SYNC_KIND, progress_sync_handler
from tests.sync_env import sync_env

pytestmark = pytest.mark.req("EF-EXP-026", "EF-INT-017", "EF-INT-018")


def context() -> Any:
    counters: dict[str, object] = {}

    def add(**counts: int) -> None:
        for name, amount in counts.items():
            counters[name] = int(str(counters.get(name, 0))) + amount

    return SimpleNamespace(
        counters=counters, record=counters.update, add=add, stop_requested=False,
        stop=lambda: False,
    )  # fmt: skip


def test_the_job_is_a_background_kind_and_runs_in_the_lowest_class() -> None:
    assert PROGRESS_SYNC_KIND == "progress.sync"
    assert traits_of(PROGRESS_SYNC_KIND).background
    assert priority_class_of(PROGRESS_SYNC_KIND, windowed=False) == "backfill"
    assert priority_class_of("export.run", windowed=False) == "new"
    assert not traits_of(PROGRESS_SYNC_KIND).security_repair  # a pause parks it, as any job


def test_the_job_synchronises_every_enabled_destination(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, 8)
        env.clock.advance(minutes=1)
        ctx = context()
        progress_sync_handler(env.engine, lambda: env.remote, env.clock)(ctx)
        assert env.kavita_page(reader, chapter) == 8
        assert ctx.counters["synchronised"] == 1


def test_kavita_down_is_a_pause_in_the_report_and_never_an_exception(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, 8)
        env.fake.asleep = True
        ctx = context()
        progress_sync_handler(env.engine, lambda: env.remote, env.clock)(ctx)  # raises nothing
        assert ctx.counters["paused"] == 1
        assert "failed" not in ctx.counters


def test_a_locked_keyring_leaves_the_job_idle(tmp_path: Path) -> None:
    from shelfhold.composition import KeyringUnavailableError

    with sync_env(tmp_path) as env:

        def locked() -> Any:
            raise KeyringUnavailableError

        ctx = context()
        progress_sync_handler(env.engine, locked, env.clock)(ctx)
        assert ctx.counters["skipped"] == "keyring_unavailable"
