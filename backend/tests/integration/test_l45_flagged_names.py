"""The contested names of a destination (M3 L45, EF-EXP-005): the screen of the shelves opens
the naming preview by itself when the server counts names the template of the destination makes
collide, truncate or that it refuses. The count is made from the database alone, with the
template stored in the destination, and no Kavita is asked."""

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.ops.detach import DetachOps
from tests.l50_env import seeded

pytestmark = pytest.mark.req("EF-EXP-005")


def _service(env: Any) -> rj.PlanService:
    comp = env.inst.comp
    pipeline, _reconciler, _fsops = rj.build_pipeline(comp, env.scans)
    return rj.PlanService(comp, lambda: pipeline, lambda: None)


def test_clean_names_count_for_nothing_and_two_series_of_one_name_are_counted(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        env.add_chapter(env.new_series("Dr Stone"), "1")
        service = _service(env)
        assert service.flagged_names(env.destination_id) == 0
        env.add_chapter(env.new_series("Dr Stone"), "1")  # the planner refuses the second one
        assert service.flagged_names(env.destination_id) >= 1


def test_a_name_too_long_for_the_disk_is_counted(settings: Settings, tmp_path: Path) -> None:
    with seeded(settings, tmp_path) as env:
        env.add_chapter(env.new_series("Long " * 80), "1")
        assert _service(env).flagged_names(env.destination_id) == 1


def test_the_listing_of_the_destinations_carries_the_count_of_its_service(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        asked: list[str] = []

        def count(destination_id: str) -> int:
            asked.append(destination_id)
            return 7

        ops = DetachOps(env.inst.comp, SimpleNamespace(), flagged_names=count)  # type: ignore[arg-type]
        [item] = ops.list_destinations()
        assert item["id"] == env.destination_id and item["flagged_names"] == 7
        assert asked == [env.destination_id]
