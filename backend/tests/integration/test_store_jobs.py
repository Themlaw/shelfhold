"""The window jobs of /store (EF-BIB-027, EF-BIB-042 (4), EF-INT-027): registered as window jobs,
and quiet on an instance that has nothing to keep."""

from typing import Any

import pytest

from shelfhold.config import Settings
from shelfhold.store_jobs import (
    KEEP_CATCHUP_KIND,
    STORE_INDEX_KIND,
    keep_catchup_handler,
    register_store_jobs,
    store_budget,
    store_index_handler,
)
from tests.l41_env import context, instance

pytestmark = pytest.mark.req("EF-BIB-042", "EF-INT-027")


class _Scheduler:
    def __init__(self) -> None:
        self.added: list[tuple[str, bool]] = []

    def add(self, kind: str, _cron: str, *, windowed: bool = False, **_: Any) -> None:
        self.added.append((kind, windowed))


class _Pools:
    def __init__(self) -> None:
        self.scheduler = _Scheduler()
        self.registered: dict[str, dict[str, Any]] = {}

    def register(self, kind: str, **options: Any) -> None:
        self.registered[kind] = options


def test_both_jobs_are_window_jobs_on_the_maintenance_pool(settings: Settings, tmp_path) -> None:
    with instance(settings, tmp_path) as inst:
        pools = _Pools()
        register_store_jobs(pools, inst.comp)  # type: ignore[arg-type]
        assert set(pools.registered) == {KEEP_CATCHUP_KIND, STORE_INDEX_KIND}
        assert all(o["windowed"] and o["pool"] == "maint" for o in pools.registered.values())
        assert sorted(pools.scheduler.added) == sorted(
            [(KEEP_CATCHUP_KIND, True), (STORE_INDEX_KIND, True)]
        )


def test_the_jobs_return_quietly_on_an_instance_with_nothing_to_keep(
    settings: Settings, tmp_path
) -> None:
    with instance(settings, tmp_path) as inst:
        keep = context(KEEP_CATCHUP_KIND)
        keep_catchup_handler(inst.comp)(keep)  # no keep shelf: not even the disk is measured
        assert keep.counters == {}
        index = context(STORE_INDEX_KIND)
        store_index_handler(inst.comp)(index)  # no library yet
        assert index.counters.get("skipped") is not None


def test_the_budget_is_the_free_space_above_the_disk_guard_reserve(
    settings: Settings, tmp_path
) -> None:
    with instance(settings, tmp_path) as inst:
        budget = store_budget(inst.comp)
        assert budget.reserve_bytes >= int(settings.disk_pause_gib * 1024**3)
        assert budget.room == max(0, budget.free_bytes - budget.reserve_bytes)
