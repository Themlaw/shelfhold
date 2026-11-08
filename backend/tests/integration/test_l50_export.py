"""L50: series cover, template copied into the binding, no disk access before a non-empty plan,
the maintenance window, the locks held for a real export, the frozen path. Everything goes
through the production wiring (jobs, pipeline, lease); only Kavita is the simulated server."""

import asyncio
import json
import threading
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.root_bootstrap import RootPolicy
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE, NamingTemplate
from shelfhold.export.domain.path_map import PathMode
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, coherence, export
from shelfhold.jobs.huey_app import JobPools
from tests.fakes_clock import ManualClock
from tests.fs_spy import FsSpy
from tests.headless_env import headless
from tests.l41_env import context
from tests.l50_env import REMOTE_ROOT, Seeded, seeded


def png(number: str) -> bytes:
    return b"\x89PNG\r\n\x1a\n" + number.encode() + b"\x00" * 16


def index_in_kavita(env: Seeded, series_title: str = "Dr Stone") -> int:
    """Kavita scans the folder and finds what the export wrote (the simulated server)."""
    folder = f"{REMOTE_ROOT}/{series_title}"
    files = [f"{REMOTE_ROOT}/{r.rel_path}" for r in env.rows(export.export_record)]
    env.inst.kavita.discover(folder, files)
    return int(next(s["id"] for s in env.inst.kavita.series if s["name"] == series_title))


def settle(env: Seeded) -> None:
    client = env.inst.comp.kavita_client(rj.tree_destinations(env.inst.comp)[0].target_id)
    asyncio.run(rj.settle_arrival(env.inst.comp, client, env.destination_id))


# -- EF-EXP-045: the cover of the series, posted by the API from chapter 1 -------------------------


@pytest.mark.req("EF-EXP-045")
def test_series_cover_uploaded_from_chapter_one_after_index(settings: Settings, tmp_path) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("0", "2", "1"):
            env.add_chapter(series, number)
        env.export()
        settle(env)
        assert env.inst.kavita.covers == {}  # Kavita has not indexed the folder yet
        series_id = index_in_kavita(env)
        settle(env)
        assert env.inst.kavita.covers == {series_id: png("1")}  # chapter 1, never the special 0
        assert env.inst.kavita.violations == []  # bare base64, no `data:` prefix
        puts = len(env.inst.kavita.requests_to("/Upload/series"))
        settle(env)
        assert len(env.inst.kavita.requests_to("/Upload/series")) == puts  # once, not every run


@pytest.mark.req("EF-EXP-045")
def test_series_cover_moves_to_chapter_one_when_it_arrives_after_chapter_two(
    settings: Settings, tmp_path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.export()
        series_id = index_in_kavita(env)
        settle(env)
        assert env.inst.kavita.covers == {series_id: png("2")}
        env.add_chapter(series, "1")
        env.export()
        index_in_kavita(env)
        settle(env)
        assert env.inst.kavita.covers == {series_id: png("1")}


@pytest.mark.req("EF-EXP-045", "EF-EXP-057")
def test_series_cover_never_covers_over_a_cover_the_user_chose(
    settings: Settings, tmp_path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("1", "2"):
            env.add_chapter(series, number)
        env.export()
        series_id = index_in_kavita(env)
        env.inst.kavita.set_user_cover(series_id, b"the user's own cover")
        settle(env)
        assert env.inst.kavita.covers == {series_id: b"the user's own cover"}
        assert env.inst.kavita.requests_to("/Upload/series") == []  # nothing was even sent


@pytest.mark.req("EF-EXP-045", "EF-EXP-057")
def test_chapter_one_arriving_after_two_leaves_a_cover_the_user_chose(
    settings: Settings, tmp_path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.export()
        series_id = index_in_kavita(env)
        env.inst.kavita.set_user_cover(series_id, b"the user's own cover")
        settle(env)
        env.add_chapter(series, "1")
        env.export()
        index_in_kavita(env)
        settle(env)
        assert env.inst.kavita.covers == {series_id: b"the user's own cover"}
        assert env.inst.kavita.requests_to("/Upload/series") == []


@pytest.mark.req("EF-EXP-045")
def test_a_lost_page_is_skipped_remembered_and_not_read_again(settings: Settings, tmp_path) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.export()
        series_id = index_in_kavita(env)
        for archive in (env.inst.library / ".shelfhold").rglob("*.cbz"):
            archive.unlink()  # the received archive is gone
        settle(env)  # does not raise: the next destinations would go on
        assert env.inst.kavita.covers == {}
        with FsSpy(watched(env)).watching() as spy:
            settle(env)
        assert spy.accesses == []  # remembered: the page is not looked for every 5 minutes
        assert series_id not in env.inst.kavita.covers


@pytest.mark.req("EF-EXP-045")
def test_series_with_only_chapter_zero_gets_no_cover(settings: Settings, tmp_path) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "0")
        env.export()
        index_in_kavita(env)
        settle(env)
        assert env.inst.kavita.covers == {}


# -- EF-EXP-004: the template is copied into the binding by `shelf create` -------------------------


@pytest.mark.req("EF-EXP-004")
def test_template_copied_into_binding_with_origin(settings: Settings, tmp_path) -> None:
    root = tmp_path / "library" / "manga"
    root.mkdir(parents=True)
    with headless(
        settings, roots=RootPolicy(tmp_path / "library", settings.data_dir, settings.store_dir)
    ) as env:
        env.admin.target_add_kavita(
            name="k", url="http://kavita.internal:5000", username="u", password="synthetic",  # noqa: S106
            accept_admin_risk=True,
            path_map=[{"local": str(root), "remote": "/manga", "mode": PathMode.RW.value}],
        )  # fmt: skip
        env.admin.shelf_create(name="Adultes", max_age="18+", target="k", subpath="Kavita")
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(
                    export.shelf_destination.c.template, export.shelf_destination.c.template_origin
                )
            ).one()
    assert NamingTemplate.loads(row.template) == DEFAULT_TEMPLATE
    assert row.template_origin == f"default@{DEFAULT_TEMPLATE.version}"


@pytest.mark.req("EF-EXP-004")
def test_new_default_template_renames_nothing(settings: Settings, tmp_path) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.add_chapter(series, "3")
        env.export()
        before = sorted(p.name for p in env.exported_files())
        assert before == ["Dr Stone - Ch.2.cbz", "Dr Stone - Ch.3.cbz"]
        newer = NamingTemplate(
            template_id=DEFAULT_TEMPLATE.template_id,
            version=DEFAULT_TEMPLATE.version + 1,
            chapter="{series} Chapter {number}",
        )  # a release that changes the default
        shelves = ShelfService(env.inst.engine)
        shelf = shelves.create_shelf(name="Autre", phrase="Autre")
        other = shelves.bind_destination(
            shelf_id=shelf, target_id=rj.tree_destinations(env.inst.comp)[0].target_id,
            transport="tree", plugin_egress="foyer", root_subpath="Kavita2", default=newer,
        )  # fmt: skip
        with env.inst.engine.connect() as connection:
            origins = dict(
                connection.execute(
                    sa.select(
                        export.shelf_destination.c.id, export.shelf_destination.c.template_origin
                    )
                ).all()
            )
        assert origins[other] == f"default@{newer.version}"
        assert origins[env.destination_id] == f"default@{DEFAULT_TEMPLATE.version}"
        assert env.export().ops == ()  # the first binding keeps its copy: nothing to write
        assert sorted(p.name for p in env.exported_files()) == before
        refs = {r.template_ref for r in env.rows(export.export_record)}
        assert refs == {DEFAULT_TEMPLATE.ref}  # the manifest still says which template named them


# -- ENF-OPS-014: no disk access before a non-empty plan ------------------------------------------


def watched(env: Seeded) -> list[Path]:
    return [env.inst.library, env.inst.settings.store_dir]


@pytest.mark.req("ENF-OPS-014")
def test_export_cron_with_empty_plan_touches_no_file_nor_lease(
    settings: Settings, tmp_path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        with FsSpy(watched(env)).watching() as busy:
            env.export()  # a non-empty plan does reach the disks: the spy sees it
        assert busy.accesses
        lease = env.inst.library / ".shelfhold" / "lease"
        assert not lease.exists()
        before = sorted(p.name for p in env.inst.library.rglob("*"))
        with FsSpy(watched(env)).watching() as spy:
            rj.export_handler(env.inst.comp)(context("export.run"))  # the cron, plan empty
        assert spy.accesses == []
        assert not lease.exists()
        assert sorted(p.name for p in env.inst.library.rglob("*")) == before


@pytest.mark.req("ENF-OPS-014")
def test_export_cron_on_a_fresh_shelf_touches_no_file(settings: Settings, tmp_path) -> None:
    with seeded(settings, tmp_path) as env:
        with FsSpy(watched(env)).watching() as spy:
            rj.export_handler(env.inst.comp)(context("export.run"))
        assert spy.accesses == []


# -- ENF-OPS-014: the verification of the tree is a window job ------------------------------------


@pytest.mark.req("ENF-OPS-014")
def test_reconcile_scan_only_in_window(settings: Settings, tmp_path) -> None:
    """A day of the real scheduler: the periodic jobs run, and the disks are looked at only
    between 03:00 and 07:00 (Paris), never by the 5 and 10 minute crons."""
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        env.add_chapter(series, "3")
        env.export()
        lost = env.exported_files()[0]
        lost.unlink()  # deleted by hand: only the window verification will see it
        start = datetime(2026, 11, 6, 6, 0, tzinfo=UTC)  # 07:00 in Paris: the window just closed
        clock = ManualClock(start)
        pools = JobPools(
            env.inst.settings.data_dir, engine=env.inst.engine, clock=clock, seed="l50"
        )
        rj.register_runtime_jobs(pools, env.inst.comp)
        ran: dict[str, list[int]] = {}
        outside: list[str] = []
        inside = 0
        for _ in range(24 * 4):  # a quarter of an hour at a time, for a day
            clock.advance(minutes=15)
            queued = pools.scheduler.scan().queued
            with FsSpy(watched(env)).watching() as spy:
                for job in queued:
                    pools._execute(job.job_id)
                    ran.setdefault(job.kind, []).append(1)
            paris_hour = (clock.now() + timedelta(hours=1)).hour
            if 3 <= paris_hour < 7:
                inside += len(spy.accesses)
            else:
                outside.extend(spy.accesses)
        assert outside == []
        assert inside > 0  # the windowed verification did look at the disk
        assert lost.is_file()  # the drift was seen in the window, planned again, written again
        assert len(ran["export.run"]) >= 90  # the cron ran all day
        assert "reconcile.cycle" in ran and "reconcile.hash" in ran
        with env.inst.engine.connect() as connection:
            failed = connection.execute(
                sa.text("SELECT kind, error_code FROM job_view WHERE error_code IS NOT NULL")
            ).all()
        assert failed == []


# -- EF-EXP-042: a real lock held by another job serializes the export ---


@pytest.mark.req("EF-EXP-042", "ENF-QUAL-006")
def test_concurrent_export_and_rename_same_series_serialized(settings: Settings, tmp_path) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        env.add_chapter(series, "2")
        locks = ResourceLocks(env.inst.engine, clock=env.inst.comp.clock)
        outcome: list[object] = []
        worker = threading.Thread(target=lambda: outcome.append(env.export()))
        with locks.hold(
            new_ulid(), work=new_ulid(), series=f"{env.destination_id}/Dr Stone", timeout=5
        ):  # a rename plan of this series folder holds its path lock
            worker.start()
            time.sleep(0.6)
            assert worker.is_alive()  # the export waits, it does not write beside the rename
            assert env.exported_files() == []
            with env.inst.engine.connect() as connection:
                held = connection.execute(
                    sa.select(sa.func.count()).select_from(coherence.resource_lock)
                ).scalar_one()
            assert held == 2  # the rename's work and path locks only
        worker.join(30)
        assert not worker.is_alive()
        assert [p.name for p in env.exported_files()] == ["Dr Stone - Ch.2.cbz"]
        with env.inst.engine.connect() as connection:
            left = connection.execute(
                sa.select(sa.func.count()).select_from(coherence.resource_lock)
            ).scalar_one()
        assert left == 0


# -- EF-EXP-061: a late volume or a corrected title never renames ---------------------------------


@pytest.mark.req("EF-EXP-061")
def test_late_volume_and_corrected_title_rename_nothing_under_the_production_wiring(
    settings: Settings, tmp_path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        units = [env.add_chapter(series, str(n)) for n in range(1, 6)]
        env.export()
        files = {p.name: p.stat().st_ino for p in env.exported_files()}
        with env.inst.engine.begin() as connection:
            volume = new_ulid()
            connection.execute(
                sa.insert(catalog.volume).values(
                    id=volume, edition_id=series.edition_id, number="1"
                )
            )
            for n, unit in enumerate(units):
                connection.execute(
                    sa.update(catalog.unit)
                    .where(catalog.unit.c.id == unit)
                    .values(volume_id=volume, title=f"Titre corrige {n}")
                )
        pipeline, _reconciler, _fsops = rj.build_pipeline(env.inst.comp, env.scans)
        preview = pipeline.preview(env.destination_id)
        assert preview.ops == ()
        assert {w.code for w in preview.warnings} == {"sidecar_changed"}
        assert env.export().ops == ()
        assert {p.name: p.stat().st_ino for p in env.exported_files()} == files
        with env.inst.engine.connect() as connection:
            ops = set(connection.execute(sa.text("SELECT op FROM fs_intent")).scalars())
        assert "rename" not in ops
        assert json.dumps(sorted(files)) == json.dumps(sorted(p.name for p in env.exported_files()))


# -- EF-EXP-046: the cover of a series whose first page is AVIF -----------------------------------


def avif_page(colour: tuple[int, int, int] = (200, 10, 10)) -> bytes:
    import io

    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", (64, 48), colour).save(out, "AVIF")
    return out.getvalue()


@pytest.mark.req("EF-EXP-046")
def test_avif_first_page_cover_set_as_jpeg_or_webp(settings: Settings, tmp_path) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        avif = avif_page()
        env.add_chapter(series, "1", page=("001.avif", avif))
        env.export()
        series_id = index_in_kavita(env)
        settle(env)
        cover = env.inst.kavita.covers[series_id]
        assert cover[:4] == b"RIFF" and cover[8:12] == b"WEBP"  # WebP, decoded in the sandbox
        assert env.inst.kavita.violations == []
        (exported,) = env.exported_files()
        with zipfile.ZipFile(exported) as archive:  # the pages are inchanged, still AVIF
            assert archive.read("001.avif") == avif


@pytest.mark.req("EF-EXP-046")
def test_undecodable_avif_cover_is_refused_by_the_worker_and_not_posted(
    settings: Settings, tmp_path
) -> None:
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        broken = b"\x00\x00\x00\x20ftypavif\x00\x00\x00\x00avifmif1miaf" + b"\xff" * 64
        env.add_chapter(series, "1", page=("001.avif", broken))
        env.export()
        series_id = index_in_kavita(env)
        settle(env)
        assert series_id not in env.inst.kavita.covers  # nothing undecodable reaches Kavita
        puts = len(env.inst.kavita.requests_to("/Upload/series"))
        settle(env)
        assert len(env.inst.kavita.requests_to("/Upload/series")) == puts == 0  # not retried
