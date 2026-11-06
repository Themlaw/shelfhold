"""ENF-OPS-014 (what L50 left): a pass with nothing to write wakes no disk. The follow cycle
takes no lease on `/library` when it has nothing to file, and a candidate blocked by an occupied
destination is not looked for on the disk again at every pass."""

from __future__ import annotations

from pathlib import Path

import pytest

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.export.application.snapshot import load_context
from shelfhold.jobs.huey_app import JobPools
from tests.fs_spy import FsSpy
from tests.l41_env import context, instance
from tests.l50_env import Seeded, seeded


def watched(library: Path, store: Path) -> list[Path]:
    return [library, store]


@pytest.mark.req("ENF-OPS-014")
def test_follow_cycle_with_nothing_to_file_takes_no_lease_and_touches_no_disk(
    settings: Settings, tmp_path: Path
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure(suwayomi=True)
        pools = JobPools(settings.data_dir, engine=inst.engine, clock=inst.comp.clock, seed="l52")
        run = rj.follow_handler(inst.comp, pools)
        run(context("follow.cycle"))  # the first pass settles what is new in the installation
        lease = inst.library / ".shelfhold" / "lease"
        before = sorted(p.name for p in inst.library.rglob("*"))
        with FsSpy(watched(inst.library.parent, settings.store_dir)).watching() as spy:
            run(context("follow.cycle"))  # the 15-minute cron: Suwayomi has nothing new
        assert spy.accesses == []  # not a stat, not the lease, not a sentinel
        assert not lease.exists()
        assert sorted(p.name for p in inst.library.rglob("*")) == before


def occupy(env: Seeded) -> Path:
    """The path the next chapter would take, held by a file that is not Shelfhold's."""
    pipeline, _reconciler, _fsops = rj.build_pipeline(env.inst.comp, env.scans)
    from tests.fakes.kavita.plugin_parse import simulate_parse

    pipeline.bind_parse(simulate_parse, verified=True)
    rel = pipeline.preview(env.destination_id).ops[0].rel_path
    assert rel is not None
    with env.inst.engine.connect() as connection:
        taken = Path(load_context(connection, env.destination_id).local(rel))
    taken.parent.mkdir(parents=True, exist_ok=True)
    taken.write_bytes(b"a file somebody else put there")
    return taken


@pytest.mark.req("ENF-OPS-014")
def test_candidate_blocked_by_an_occupied_destination_does_not_wake_the_disks_again(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        taken = occupy(env)
        watch = watched(env.inst.library, env.inst.settings.store_dir)
        with FsSpy(watch).watching() as first:
            assert env.export().ops == ()  # blocked: `export.destination_occupied`
        assert first.accesses  # the first pass has to look: that is how it learns
        assert taken.read_bytes() == b"a file somebody else put there"
        for _ in range(3):  # the 5-minute cron, three times
            with FsSpy(watch).watching() as spy:
                assert env.export().ops == ()
            assert spy.accesses == []
        with FsSpy(watch).watching() as cron:  # the real cron: Kavita is not asked either
            rj.export_handler(env.inst.comp)(context("export.run"))
        assert cron.accesses == []
        assert not (env.inst.library / ".shelfhold" / "lease").exists()
        # the window verification looks at the disks anyway: it forgets what was learned, so a
        # file removed by hand is exported at the next pass
        taken.unlink()
        rj.reconcile_handler(env.inst.comp, in_window=True)(context("reconcile.cycle"))
        assert len(env.export().ops) == 1
        assert len(env.exported_files()) == 1


@pytest.mark.req("ENF-OPS-014")
def test_a_new_chapter_is_looked_at_even_when_another_one_is_blocked(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        taken = occupy(env)
        assert env.export().ops == ()
        env.add_chapter(series, "3")  # the inputs changed: the destination is looked at again
        report = env.export()
        assert [op.kind for op in report.ops] == ["write"]
        assert len(env.exported_files()) == 2  # the foreign file, left alone, and the new chapter
        assert taken.read_bytes() == b"a file somebody else put there"


@pytest.mark.req("ENF-OPS-014")
def test_a_blocked_name_that_depends_on_kavita_is_not_remembered(
    settings: Settings, tmp_path: Path
) -> None:
    """Only "the destination is taken" is learned from the disk: a name Kavita misreads can
    change with Kavita alone, so the next pass asks again."""
    from dataclasses import replace

    from shelfhold.export.application import blocked_memo
    from tests.fakes.kavita.plugin_parse import simulate_parse

    def misread(name: str) -> object:
        parsed = simulate_parse(name)
        return replace(parsed, min_chapter=99.0, max_chapter=99.0)

    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        pipeline, _reconciler, _fsops = rj.build_pipeline(env.inst.comp, env.scans)
        pipeline.bind_parse(misread, verified=True)  # type: ignore[arg-type]
        assert pipeline.run(env.destination_id, job_id="job").ops == ()
        preview = pipeline.preview(env.destination_id)
        assert [b.code for b in preview.blocked] == ["export.name_misread_number"]
        assert not blocked_memo.known(env.inst.engine, env.destination_id, preview.base_version)
