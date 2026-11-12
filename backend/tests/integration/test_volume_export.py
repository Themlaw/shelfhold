"""M2 L26b (EF-BIB-040, EF-SRC-032, EF-BIB-044): the web-novel volumes leave through the pipeline.

L26 made the policy and `RangedExporter`; here the volume is an export item like any other: planned
by the one planner (age gate of the EPUB series, barrier of a family shelf, names), placed by the
pipeline, replaced (rolling tail) only by the same-path upgrade. Each test attacks one rule: a 16+
volume on a family shelf, the first EPUB of a series for an account that includes unknowns, a
volume still open, a read tail, a truncated tail, a Unit exported twice, a shelf below 18+ after
an age moved. The pipeline, the database and the files are the production ones; Kavita is the
simulated server (no network).
"""

import io
import json
import zipfile
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.pipeline import SourceMissingError, wanted_names
from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.upgrade import SamePathUpgrade, UpgradeRequest
from shelfhold.export.application.withdrawal import WithdrawalService
from shelfhold.export.domain.planner import AGE_WAIT
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.infrastructure.volumes import write_data_file
from shelfhold.library.adapters.origin_opf import origin_opf_rel
from shelfhold.library.public import PassthroughStore, own_export_of, received_content_hash
from tests.epub_export_env import (
    TEEN,
    UNKNOWN,
    books_library,
    export_round,
    kavita_scans,
    open_gate,
    visible_to_restricted,
    written,
)
from tests.export_env import ExportEnv, Series, export_env, sha, state_of
from tests.fakes.kavita.server import member
from tests.family_env import NOW, admin, family_env, rate
from tests.integration.test_schema_catalog import add_variant
from tests.integration.test_web_novel_volumes import read_by_a_reader
from tests.kavita_export_env import LIBRARY_ID, Wired, wire
from tests.library_env import cbz_bytes
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED
from tests.ranged_env import RangedBench, ranged_bench, regrow, volume_epub

pytestmark = pytest.mark.req("EF-BIB-040", "EF-SRC-032", "EF-BIB-044")


def run(env: ExportEnv) -> int:
    return len(env.pipeline.run(env.destination_id, job_id=new_ulid()).ops)


def blocked(env: ExportEnv) -> list[str]:
    return sorted(b.code for b in env.pipeline.preview(env.destination_id).blocked)


def warned(env: ExportEnv) -> list[str]:
    return sorted(w.code for w in env.pipeline.preview(env.destination_id).warnings)


def record(env: ExportEnv, item_id: str) -> sa.Row[tuple[object, ...]]:
    table = export.export_record
    with env.engine.connect() as connection:
        return connection.execute(sa.select(table).where(table.c.export_item_id == item_id)).one()


def upgrades(env: ExportEnv) -> int:
    return len(env.rows(export.export_upgrade))


def binned(env: ExportEnv) -> list[Path]:
    folder = env.lib.library / ".shelfhold" / "recycle"
    return sorted(p for p in folder.rglob("*") if p.is_file()) if folder.exists() else []


def digest(path: Path) -> str:
    return sha(path.read_bytes())


def volume_on_shelf(env: ExportEnv, count: int = 3, **kw: object) -> tuple[Series, RangedBench]:
    series = env.main_series()
    return series, ranged_bench(env.lib, count, **kw)  # type: ignore[arg-type]


def give_chapter_file(env: ExportEnv, series: Series, unit_id: str) -> None:
    """The Unit also has a CBZ of its own, received as a chapter (another route)."""
    with env.engine.begin() as connection:
        variant = add_variant(connection, unit_id, series.binding_id, f"ch-{new_ulid()}")
    pages = (("1-001.png", b"\x89PNG\r\n\x1a\n" + unit_id.encode() + b"\x00" * 16),)
    PassthroughStore(
        env.engine, env.lib.fsops, env.lib.locate, env.lib.data, tombstones=NONE_PURGED,
        hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=unit_id, variant_id=variant, staged=env.lib.stage(cbz_bytes(pages)),
        dest=str(env.lib.manga / "received" / f"{unit_id}.cbz"),
    )  # fmt: skip


# -- the way out ------------------------------------------------------------------------------


def test_closed_volume_reaches_the_destination_through_the_pipeline() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 3, closed=True)
        (op,) = env.pipeline.run(env.destination_id, job_id=new_ulid()).ops
        assert op.export_item_id == bench.item_id and op.unit_id is None
        (path,) = env.files()
        assert path.name.endswith(" - Vol.1.epub")
        line = record(env, bench.item_id)
        assert (line.state, line.format, line.spine_count) == ("written", "epub", 3)
        assert line.sidecar_sha256 == digest(bench.kept())  # the kept file it was made from
        assert line.content_hash == received_content_hash(path, ".epub")  # the payload (anti-echo)
        assert line.file_hash == digest(path)
        with zipfile.ZipFile(path) as archive:  # the chapters are the ones kept, byte for byte
            kept = zipfile.ZipFile(bench.kept())
            assert b"text of ch2.xhtml" in archive.read("OEBPS/ch2.xhtml")
            assert sorted(archive.namelist()) == sorted(kept.namelist())
        items = env.rows(export.export_item)  # no second item: the one the keeper made
        assert [i.id for i in items] == [bench.item_id] and len(env.rows(export.unit_coverage)) == 3
        before = state_of(env)
        assert run(env) == 0 and state_of(env) == before  # idempotent: nothing moves


@pytest.mark.req("EF-BIB-040", "EF-SRC-032")
def test_names_of_a_closed_volume_are_asked_before_any_disk_is_read() -> None:
    """The export job asks which names the plan needs through the module function, from the
    database alone: a volume is never "missing its file" there, or no name is asked, the job does
    nothing, and the closed volume never leaves (M2 L32)."""
    with export_env() as env:
        volume_on_shelf(env, 3, closed=True)
        names, _kind = wanted_names(env.engine, env.destination_id)
        assert names != []


def test_volume_named_from_its_label_and_a_volume_without_number_is_refused() -> None:
    with export_env() as env:
        volume_on_shelf(env, 2, closed=True, label="Volume 12")
        run(env)
        (path,) = env.files()
        assert path.name.endswith(" - Vol.12.epub")
    for label in ("Prologue", None):  # a title, or no label at all (a bundle)
        with export_env() as env:
            _, _bench = volume_on_shelf(env, 2, closed=True, label=label)
            assert blocked(env) == ["export.volume_unnamed"]
            assert run(env) == 0 and env.files() == [] and env.rows(export.export_record) == []


def test_two_volumes_of_one_edition_take_two_names() -> None:
    with export_env() as env:
        _series, _first = volume_on_shelf(env, 2, closed=True, label="Vol.1")
        second = ranged_bench(env.lib, 2, closed=True, label="Vol.2")
        assert run(env) == 2
        assert [p.name.rsplit(" - ", 1)[1] for p in env.files()] == ["Vol.1.epub", "Vol.2.epub"]
        clash = ranged_bench(env.lib, 2, closed=True, label="Vol.2")  # the same number again
        assert blocked(env) == ["export.destination_occupied"]
        assert run(env) == 0 and clash.item_id not in {
            r.export_item_id for r in env.rows(export.export_record)
        }
        assert second.item_id in {r.export_item_id for r in env.rows(export.export_record)}


def test_pull_destination_gets_the_manifest_line_of_a_volume_and_no_file() -> None:
    with export_env(transport="pull") as env:
        _, bench = volume_on_shelf(env, 2, closed=True)
        assert run(env) == 1 and env.files() == []
        line = record(env, bench.item_id)
        assert (line.locator_type, line.state) == ("none", "written")


def test_volume_of_an_edition_off_the_shelf_is_never_exported() -> None:
    with export_env() as env:
        ranged_bench(env.lib, 2, closed=True)  # the Edition is filed on no shelf of this export
        assert run(env) == 0 and env.files() == [] and env.rows(export.export_record) == []


# -- the policy -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("policy", "status", "code"),
    [
        ("closed_only", "ongoing", "volume_waits"),
        ("closed_only", "finished", "volume_waits"),
        ("whole", "ongoing", "volume_refused"),
    ],
)
def test_open_volume_is_not_planned_under_closed_only_or_an_unfinished_whole(
    policy: str, status: str, code: str
) -> None:
    with export_env() as env:
        volume_on_shelf(env, 2, closed=False, policy=policy, status=status)
        assert code in warned(env)
        assert run(env) == 0 and env.files() == [] and env.rows(export.export_record) == []


def test_whole_policy_exports_only_the_closed_file_of_a_finished_novel() -> None:
    with export_env() as env:
        volume_on_shelf(env, 2, closed=True, policy="whole", status="ongoing")
        assert run(env) == 0 and env.files() == []
        ranged_bench(env.lib, 2, closed=True, policy="whole", status="finished", label="Vol.2")
        assert run(env) == 2 and len(env.files()) == 2  # a finished novel: both closed files go


def test_closed_volume_is_frozen_the_kept_bytes_never_replace_it() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 3, closed=True)
        run(env)
        (path,) = env.files()
        served = digest(path)
        regrow(bench, 3, data=volume_epub(3, extra_items=(2,)))  # the source rebuilt it
        assert run(env) == 0 and digest(path) == served
        assert upgrades(env) == 0 and binned(env) == []


def test_rolling_tail_is_replaced_at_the_same_path_by_the_upgrade() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 2, closed=False, policy="rolling_tail")
        assert run(env) == 1
        (path,) = env.files()
        first = digest(path)
        assert record(env, bench.item_id).spine_count == 2
        regrow(bench, 3)  # a chapter joined the volume
        assert run(env) == 1
        assert env.files() == [path] and digest(path) != first  # the same name, a new file
        line = record(env, bench.item_id)
        assert (line.state, line.spine_count) == ("written", 3)
        assert line.sidecar_sha256 == digest(bench.kept()) and line.file_hash == digest(path)
        assert upgrades(env) == 1 and len(binned(env)) == 1  # the old file is in the bin
        assert digest(binned(env)[0]) == first
        with env.engine.connect() as connection:
            copies = connection.execute(
                sa.text("SELECT count(*) FROM unit_coverage WHERE carrier_id = :i"),
                {"i": bench.item_id},
            ).scalar_one()
        assert copies == 3
        before = state_of(env)
        assert run(env) == 0 and state_of(env) == before  # up to date: nothing more


def test_rolling_tail_never_replaces_a_volume_a_reader_finished() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 2, closed=False, policy="rolling_tail")
        run(env)
        (path,) = env.files()
        served = digest(path)
        read_by_a_reader(env.lib, bench.item_id)
        regrow(bench, 3)
        assert run(env) == 0 and digest(path) == served
        assert upgrades(env) == 0 and binned(env) == []
        assert record(env, bench.item_id).spine_count == 2  # what is served, still


def test_open_volume_under_a_policy_without_tail_is_never_rewritten() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 2, closed=True, policy="closed_only")
        run(env)
        with env.engine.begin() as connection:  # reopened by the source, same policy
            connection.execute(sa.update(export.export_item).values(closed=False))
        (path,) = env.files()
        served = digest(path)
        regrow(bench, 3)
        assert run(env) == 0 and digest(path) == served and upgrades(env) == 0


def test_truncated_tail_trips_the_guard_and_the_served_volume_stays() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 6, closed=False, policy="rolling_tail")
        run(env)
        (path,) = env.files()
        served = digest(path)
        regrow(bench, 2)  # the source now sends a third of the book, a complete-looking TOC
        assert run(env) == 0  # nothing replaced, and the rest of the plan is not stopped
        assert digest(path) == served and upgrades(env) == 0 and binned(env) == []
        line = record(env, bench.item_id)
        assert (line.state, line.spine_count) == ("written", 6)


def test_stubbed_chapter_in_the_new_tail_trips_the_guard() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 4, closed=False, policy="rolling_tail")
        run(env)
        (path,) = env.files()
        served = digest(path)
        with env.engine.begin() as connection:  # the source only lists it now (EF-BIB-024)
            connection.execute(
                sa.text("UPDATE unit SET availability = 'stubbed' WHERE id = :u"),
                {"u": bench.unit_ids[1]},
            )
        regrow(bench, 4, data=volume_epub(4, extra_items=(2,)))  # same length, other bytes
        assert run(env) == 0 and digest(path) == served and upgrades(env) == 0


def test_irreplaceable_tail_is_not_replaced_without_the_identifier_typed() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 2, closed=False, policy="rolling_tail")
        run(env)
        (path,) = env.files()
        served = digest(path)
        with (
            env.engine.begin() as connection
        ):  # no binding lists the chapters any more (EF-BIB-031)
            connection.execute(sa.update(catalog.unit_variant).values(removed_at=env.clock.now()))
        regrow(bench, 3)
        assert run(env) == 0 and digest(path) == served and upgrades(env) == 0


def test_chapter_upgrade_door_refuses_a_volume() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 2, closed=False, policy="rolling_tail")
        run(env)
        door = SamePathUpgrade(
            env.engine, env.lib.fsops, locate=env.lib.locate, data_dir=env.lib.data, clock=env.clock
        )
        with pytest.raises(LookupError):  # a chapter's content would replace the whole volume
            door.upgrade(
                UpgradeRequest(env.destination_id, bench.item_id, new_ulid(), "better_version")
            )
        assert door.upgrade_unit(bench.unit_ids[0], new_ulid()) == []  # and no door by one Unit
        assert upgrades(env) == 0


def test_kept_file_changed_between_the_plan_and_the_write_is_not_written() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 3, closed=True)
        preview = env.pipeline.preview(env.destination_id)
        write_data_file(env.lib.data, f"ranged/{bench.item_id}.epub", volume_epub(3, cover=True))
        with pytest.raises(SourceMissingError):
            env.pipeline.apply(preview.plan_id, if_match=preview.base_version, job_id=new_ulid())
        assert env.files() == []
        assert [r.state for r in env.rows(export.export_record)] in ([], ["planned"])


def test_a_refused_volume_does_not_hold_back_the_chapters_of_the_plan() -> None:
    with export_env() as env:
        series, _ = volume_on_shelf(
            env, 3, closed=True, data=volume_epub(3, nav=[("Chapter 1", "ch1.xhtml")])
        )
        env.add_chapter(series, "900")
        assert run(env) == 1 and [p.suffix for p in env.files()] == [".cbz"]


def test_the_original_opf_of_a_volume_is_kept_under_its_first_unit() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 2, closed=True)
        run(env)
        assert (env.lib.data / origin_opf_rel(bench.unit_ids[0])).is_file()


def test_volume_with_a_hole_in_its_table_of_contents_is_never_written() -> None:
    with export_env() as env:
        nav = [("Chapter 1", "ch1.xhtml"), ("Chapter 3", "ch3.xhtml")]
        volume_on_shelf(env, 3, closed=True, data=volume_epub(3, nav=nav))
        assert run(env) == 0 and env.files() == [] and env.rows(export.export_record) == []


def test_missing_kept_file_blocks_the_volume_with_a_code() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 2, closed=True)
        bench.kept().unlink()
        assert blocked(env) == ["export.ranged_source_missing"]
        assert run(env) == 0 and env.files() == []


# -- one Unit, one file -----------------------------------------------------------------------


def test_unit_covered_by_a_volume_is_not_also_exported_as_a_chapter() -> None:
    with export_env() as env:
        series, bench = volume_on_shelf(env, 3, closed=True)
        give_chapter_file(env, series, bench.unit_ids[0])  # a CBZ for a Unit the volume carries
        extra = env.add_chapter(series, "900")  # a chapter nothing covers: it goes by itself
        assert run(env) == 2
        names = sorted(p.suffix for p in env.files())
        assert names == [".cbz", ".epub"]
        covered = {r.export_item_id for r in env.rows(export.export_record)}
        assert bench.item_id in covered and len(covered) == 2
        with env.engine.connect() as connection:
            units = connection.execute(
                sa.text(
                    "SELECT c.unit_id FROM export_record r JOIN unit_coverage c "
                    "ON c.carrier_id = r.export_item_id WHERE r.export_item_id != :v"
                ),
                {"v": bench.item_id},
            ).scalars()
            assert list(units) == [extra]


def test_unit_already_exported_as_a_chapter_blocks_the_volume_that_covers_it() -> None:
    with export_env() as env:
        series, bench = volume_on_shelf(env, 3, closed=True)
        covered = bench.unit_ids[0]
        item = new_ulid()
        with env.engine.begin() as connection:  # the chapter left earlier, as its own item
            connection.execute(
                sa.insert(export.export_item).values(
                    id=item,
                    edition_id=series.edition_id,
                    kind="unit",
                    frozen_attrs={"series": "Old", "track": "main", "number_norm": "1"},
                )
            )
            connection.execute(
                sa.insert(export.unit_coverage).values(
                    carrier_type="export_item", carrier_id=item, unit_id=covered, position=0,
                    extent="full", origin="manual",
                )
            )  # fmt: skip
            connection.execute(
                sa.insert(export.export_record).values(
                    shelf_destination_id=env.destination_id, export_item_id=item,
                    locator_type="path", rel_path="Old/Old 1.cbz", format="cbz", state="written",
                    content_hash="sha256:" + "b" * 64, file_hash="sha256:" + "c" * 64,
                )
            )  # fmt: skip
        assert blocked(env) == ["export.volume_unit_already_exported"]
        assert run(env) == 0 and env.files() == []


# -- the barrier of a family shelf -------------------------------------------------------------


def test_sixteen_plus_volume_never_reaches_a_family_shelf() -> None:
    with family_env(13) as env:
        w = wire(env)
        books_library(env)
        series, _ = volume_on_shelf(env, 2, closed=True)
        assert blocked(env) == ["shelf.rating_required"]  # nobody rated it
        assert export_round(w) == [] and env.files() == []
        assert env.rows(export.export_record) == []
        rate(env, series, 16)
        assert blocked(env) == ["shelf.above_ceiling"]
        assert export_round(w) == [] and env.files() == []
        assert env.rows(export.export_record) == []
        rate(env, series, 13)
        assert len(export_round(w)) == 1 and len(env.files()) == 1


def test_a_plan_stored_below_the_ceiling_is_not_applied_once_the_age_rose() -> None:
    with family_env(13) as env:
        w = wire(env)
        books_library(env)
        series, bench = volume_on_shelf(env, 2, closed=True)
        rate(env, series, 10)
        open_gate(w)
        preview = env.pipeline.preview(env.destination_id)
        assert [op.export_item_id for op in preview.ops] == [bench.item_id]
        rate(env, series, 16)
        with pytest.raises(StalePlanError):
            env.pipeline.apply(preview.plan_id, if_match=preview.base_version, job_id=new_ulid())
        assert env.files() == [] and env.rows(export.export_record) == []


def test_rolling_tail_of_a_volume_whose_age_rose_is_not_replaced_on_a_family_shelf() -> None:
    with family_env(16) as env:
        w = wire(env)
        books_library(env)
        series, bench = volume_on_shelf(env, 2, closed=False, policy="rolling_tail")
        rate(env, series, 10)
        assert len(export_round(w)) == 1
        (path,) = env.files()
        served = digest(path)
        kavita_scans(w, "")
        rate(env, series, 18)  # above the shelf now, not yet withdrawn
        regrow(bench, 3)
        assert export_round(w) == [] and digest(path) == served and upgrades(env) == 0


def test_withdrawal_after_an_age_rise_takes_the_volumes_out_too() -> None:
    with family_env(13) as env:
        w = wire(env)
        books_library(env)
        series, first = volume_on_shelf(env, 2, closed=True, label="Vol.1")
        second = ranged_bench(env.lib, 2, closed=True, label="Vol.2")
        rate(env, series, 10)
        export_round(w)
        kavita_scans(w, "")
        open_gate(w)  # the age is pushed and read back: the second volume may go
        export_round(w)
        assert len(env.files()) == 2
        rate(env, series, 16)  # the admin ruled: the children no longer read it
        service = WithdrawalService(
            env.engine, env.lib.fsops, clock=env.clock, request_scan=env.scans.append
        )
        plan = service.plan()
        (item,) = plan.items
        assert {f.export_item_id for f in item.files} == {first.item_id, second.item_id}
        report = service.apply(if_match=plan.version, principal=admin(), now=NOW)
        assert report.files == 2 and env.files() == []
        assert {r.state for r in env.rows(export.export_record)} == {"recycled"}
        assert export_round(w) == [] and env.files() == []  # and nothing comes back by itself


# -- the age gate of an EPUB series (Kavita) -----------------------------------------------------


def kid(w: Wired, *, include_unknowns: bool) -> None:
    w.fake.users.append(
        member(
            "kid", libraries=(LIBRARY_ID,), age_rating=TEEN, include_unknowns=include_unknowns
        )
    )  # fmt: skip


def test_first_volume_withheld_from_a_restricted_account_that_includes_unknowns() -> None:
    with export_env() as env:
        w = wire(env)
        books_library(env)
        series, _ = volume_on_shelf(env, 2, closed=True, label="Vol.1")
        ranged_bench(env.lib, 2, closed=True, label="Vol.2")
        rate(env, series, 16)
        kid(w, include_unknowns=True)
        assert visible_to_restricted(UNKNOWN, ceiling=TEEN, include_unknowns=True)
        assert export_round(w) == [] and env.files() == []
        (alert,) = w.events.details("target.child_exposure")
        assert alert["setting"] == "include_unknowns" and alert["accounts"] == ["kid"]
        assert blocked(env) == [AGE_WAIT, AGE_WAIT]
        w.fake.users[-1]["ageRestriction"]["includeUnknowns"] = False  # the admin fixed it
        assert len(export_round(w)) == 1  # the first volume of the series goes alone


def test_volumes_of_a_series_wait_for_the_age_to_read_back_locked() -> None:
    with export_env() as env:
        w = wire(env)
        books_library(env)
        series, _ = volume_on_shelf(env, 2, closed=True, label="Vol.1")
        ranged_bench(env.lib, 2, closed=True, label="Vol.2")
        rate(env, series, 10)
        w.fake.asleep = True  # Kavita does not answer: the gate stays closed
        assert export_round(w) == [] and env.files() == []
        w.fake.wake_up()
        (first,) = export_round(w)
        assert export_round(w) == []  # not scanned yet: no age was pushed, the second waits
        assert len(written(env)) == 1 and blocked(env) == [AGE_WAIT]
        kavita_scans(w, "Vol.1")
        (second,) = export_round(w)  # pushed, read back locked: now the rest of the series
        assert sorted(written(env)) == sorted([first, second])


# -- regressions of the review of #160 ---------------------------------------------------------


def test_chapter_a_re_cut_unit_became_is_not_exported_beside_the_volume() -> None:
    with export_env() as env:
        series, bench = volume_on_shelf(env, 3, closed=True)
        child = env.add_chapter(series, "100.1")
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(export.unit_coverage).values(
                    carrier_type="unit", carrier_id=bench.unit_ids[0], unit_id=child,
                    position=0, extent="partial", origin="provider",
                )
            )  # fmt: skip
        assert run(env) == 1 and [p.suffix for p in env.files()] == [".epub"]


def test_a_unit_in_two_volumes_leaves_in_one_only() -> None:
    with export_env() as env:
        _, first = volume_on_shelf(env, 3, closed=True)
        second = ranged_bench(env.lib, 3, closed=True, label="Vol.2")
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(export.unit_coverage).values(
                    carrier_type="export_item", carrier_id=second.item_id,
                    unit_id=first.unit_ids[0], position=99, extent="full", origin="provider",
                )
            )  # fmt: skip
        assert run(env) == 1 and len(env.files()) == 1
        assert blocked(env) == ["export.volume_unit_already_exported"]


def test_echo_of_a_served_volume_with_a_rewritten_opf_is_still_recognized() -> None:
    with export_env() as env:
        volume_on_shelf(env, 3, closed=True)
        assert run(env) == 1
        (served,) = env.files()
        payload = received_content_hash(served, ".epub")
        with env.engine.connect() as connection:
            assert own_export_of(connection, file_hash=None, content_hash=payload) is not None


def test_the_name_of_a_volume_is_not_written_into_the_shared_item() -> None:
    with export_env() as env:
        _ = volume_on_shelf(env, 2, closed=True, label="Saison 2 Vol.3")
        run(env)
        (path,) = env.files()
        assert path.name.endswith(" - Vol.3.epub")
        (item,) = env.rows(export.export_item)
        assert item.frozen_attrs == {"volume_label": "Saison 2 Vol.3"}


def test_a_restored_tail_with_no_length_served_is_not_rewritten() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 2, closed=False, policy="rolling_tail")
        run(env)
        (path,) = env.files()
        served = digest(path)
        with env.engine.begin() as connection:  # what a restore leaves: no length, no fingerprint
            connection.execute(sa.update(export.export_record).values(spine_count=None))
        regrow(bench, 3)
        assert run(env) == 0 and digest(path) == served and upgrades(env) == 0
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.export_record).values(sidecar_sha256=None))
        assert run(env) == 0 and digest(path) == served and upgrades(env) == 0


def test_replace_volume_guards_the_length_itself() -> None:
    with export_env() as env:
        _, bench = volume_on_shelf(env, 6, closed=False, policy="rolling_tail")
        run(env)
        door = SamePathUpgrade(
            env.engine, env.lib.fsops, locate=env.lib.locate, data_dir=env.lib.data, clock=env.clock
        )
        short = io.BytesIO(b"x")
        result = door.replace_volume(
            env.destination_id,
            bench.item_id,
            data=short,
            file_hash="sha256:" + "1" * 64,
            content_hash="sha256:" + "2" * 64,
            source_hash="sha256:" + "3" * 64,
            spine_count=2,
        )
        assert result.outcome.value == "suspect_revision" and upgrades(env) == 0


def test_the_mirror_carries_closed_so_a_restore_keeps_the_volume_frozen() -> None:
    with export_env() as env:
        volume_on_shelf(env, 2, closed=True)
        run(env)
        lines = [
            json.loads(line)
            for f in env.lib.tmp.rglob("*.jsonl")
            for line in f.read_text().splitlines()
        ]
        items = [e["export_item"] for e in lines if e.get("kind") == "entry"]
        assert items and all(i["closed"] is True for i in items)


def test_chapters_a_resplit_made_are_not_exported_beside_the_volume_that_carries_their_parent() -> (
    None
):
    """Review of #172: after `coverage resplit apply`, 10.1 and 10.2 are covered by their parent
    (`carrier_type = unit`) and must not leave as chapters next to the file that carries it."""
    with export_env() as env:
        series, bench = volume_on_shelf(env, 3, closed=True)
        parent = bench.unit_ids[0]
        kids = [env.add_chapter(series, "10.1"), env.add_chapter(series, "10.2")]
        with env.engine.begin() as connection:
            for position, kid in enumerate(kids):
                connection.execute(
                    sa.insert(export.unit_coverage).values(
                        carrier_type="unit",
                        carrier_id=parent,
                        unit_id=kid,
                        position=position,
                        extent="full",
                        origin="manual",
                    )
                )
        assert run(env) == 1  # the volume alone
        assert [p.suffix for p in env.files()] == [".epub"]
        assert {r.export_item_id for r in env.rows(export.export_record)} == {bench.item_id}
        before = state_of(env)
        assert run(env) == 0 and state_of(env) == before
