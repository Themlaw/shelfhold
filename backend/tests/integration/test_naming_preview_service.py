"""EF-EXP-005 through the real pipeline: the preview reads the planned Units, asks the simulated
Kavita for the names of the candidate template, and writes nothing (an FsSpy proves it)."""

from dataclasses import replace
from pathlib import Path

import pytest

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE
from tests.fs_spy import FsSpy
from tests.l50_env import seeded

TEMPLATE = replace(DEFAULT_TEMPLATE, version=2, chapter="{series} Ch {number}")


def service(env: object) -> rj.PlanService:
    comp = env.inst.comp  # type: ignore[attr-defined]
    pipeline, _reconciler, _fsops = rj.build_pipeline(comp, env.scans)  # type: ignore[attr-defined]
    return rj.PlanService(comp, lambda: pipeline, lambda: None)


@pytest.mark.req("EF-EXP-005")
def test_naming_preview_via_plugin_parse_confirms_each_name(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("1", "2"):
            env.add_chapter(series, number)
        preview = service(env).naming_preview(env.destination_id, TEMPLATE)
        assert preview.verified
        assert [e.status for e in preview.entries] == ["confirmed", "confirmed"]
        assert {e.rel_path for e in preview.entries} == {
            "Dr Stone/Dr Stone Ch 1.cbz", "Dr Stone/Dr Stone Ch 2.cbz",
        }  # fmt: skip
        assert env.inst.kavita.parse_calls  # the names went through plugin/parse


@pytest.mark.req("EF-EXP-005")
def test_naming_preview_unverified_when_kavita_down(settings: Settings, tmp_path: Path) -> None:
    with seeded(settings, tmp_path) as env:
        env.add_chapter(env.new_series(), "1")
        env.inst.kavita.asleep = True
        preview = service(env).naming_preview(env.destination_id, TEMPLATE)
        assert not preview.verified
        assert [e.status for e in preview.entries] == ["unverified"]  # never blocking


@pytest.mark.req("EF-EXP-005")
def test_naming_preview_unverified_on_a_version_the_replica_does_not_cover(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        env.add_chapter(env.new_series(), "1")
        env.inst.kavita.version = "0.9.2.0"  # updated since the last probe
        preview = service(env).naming_preview(env.destination_id, TEMPLATE)
        assert not preview.verified
        assert [e.status for e in preview.entries] == ["unverified"]


@pytest.mark.req("EF-EXP-005")
def test_number_collision_flagged_in_preview(settings: Settings, tmp_path: Path) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "10")
        env.add_chapter(series, "10", part=2)  # Kavita reads both as chapter 10
        preview = service(env).naming_preview(env.destination_id, TEMPLATE)
        assert preview.collisions
        assert all(e.flags for e in preview.entries)


@pytest.mark.req("EF-EXP-005")
def test_a_preview_writes_nothing_on_disk_nor_in_the_library(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        env.add_chapter(env.new_series(), "1")
        svc = service(env)
        before = sorted(p.relative_to(env.inst.library) for p in env.inst.library.rglob("*"))
        watch = [env.inst.library, env.inst.settings.store_dir]
        with FsSpy(watch).watching() as spy:
            svc.naming_preview(env.destination_id, TEMPLATE)
            env.inst.kavita.asleep = True
            svc.naming_preview(env.destination_id, TEMPLATE)
        writes = [a for a in spy.accesses if a.split(" ", 1)[0] in WRITERS]
        assert writes == []
        assert spy.accesses == []  # not even a stat: the names come from the database
        after = sorted(p.relative_to(env.inst.library) for p in env.inst.library.rglob("*"))
        assert after == before
        assert not (env.inst.library / ".shelfhold" / "lease").exists()


WRITERS = {"open", "mkdir", "unlink", "remove", "rmdir", "rename", "replace", "utime",
           "symlink", "link", "truncate", "chmod", "fsync"}  # fmt: skip
