"""Review of PR #101 (M1b L10): every attack on the parental barrier, by the real road.

A file that must not be in the library of the children never gets there: a withdrawal that races an
export, a ceiling raised after the link, a Kavita version nobody read, a path map that moves a
folder under another shelf, a move that fails half way.
"""

import asyncio
import dataclasses
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks, work_resource
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.target_state import DestinationPausedError
from shelfhold.export.application.withdrawal import WithdrawalBusyError, WithdrawalService
from shelfhold.export.domain.bindings import BindingError
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from shelfhold.ops.events import BusTargetEvents
from tests.fakes.kavita.server import member
from tests.family_env import NOW, admin, family_env, rate
from tests.integration.test_family_shelves_real import (
    _target_of,
    dedicated_adult_library,
    family,
    link,
)
from tests.l50_env import seeded


def _withdrawal(env, wait: float = 0.2) -> WithdrawalService:
    return WithdrawalService(
        env.engine, env.lib.fsops, clock=env.clock, request_scan=env.scans.append, lock_wait=wait
    )


@pytest.mark.req("EF-BIB-037", "EF-EXP-055")
def test_withdrawal_racing_an_export_leaves_no_file_behind() -> None:
    """The rating rises and the admin applies the plan while the export renders: the export holds
    the `work` lock, so the withdrawal waits; the barrier is read again under that lock, in the
    transaction that reserves the line, and nothing is written."""
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        real = env.pipeline._renderer.render
        raced: list[str] = []

        def render(path, xml):
            if not raced:
                rate(env, series, 18)
                service = _withdrawal(env)
                with pytest.raises(WithdrawalBusyError):  # the export holds the Work
                    service.apply(if_match=service.plan().version, principal=admin(), now=NOW)
                raced.append("busy")
            return real(path, xml)

        env.pipeline._renderer.render = render
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert raced == ["busy"] and env.files() == []  # nothing in the family library
        assert env.rows(export.export_record) == []  # and no line left `planned`


@pytest.mark.req("EF-BIB-037")
def test_withdrawal_takes_the_work_lock_and_lists_stray_records() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert len(env.files()) == 1
        with env.engine.begin() as connection:  # filed nowhere, yet its file is still there
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == series.edition_id)
                .values(shelf_id=None)
            )
        service = _withdrawal(env)
        plan = service.plan()
        assert [i.reason for i in plan.items] == ["unshelved"] and len(plan.items[0].files) == 1
        held = ResourceLocks(env.engine, clock=env.clock)
        with (
            held.hold_resources("J" * 26, [work_resource(series.work_id)]),
            pytest.raises(WithdrawalBusyError),
        ):
            service.apply(if_match=plan.version, principal=admin(), now=NOW)
        assert len(env.files()) == 1  # nothing moved while the Work was taken
        service.apply(if_match=plan.version, principal=admin(), now=NOW)
        assert env.files() == []


@pytest.mark.req("EF-EXP-055", "EF-USR-015")
def test_ceiling_raised_after_the_link_pauses_the_shelf_until_recheck(
    settings: Settings, tmp_path: Path
) -> None:
    with seeded(settings, tmp_path) as env:
        dedicated_adult_library(env)
        destination = family(env)
        ref = link(env, destination)
        inst = env.inst
        inst.kavita.users.append(member("kid", libraries=(int(ref),), age_rating=8))
        target = _target_of(env, destination)
        auditor = AccessAuditor(
            inst.engine, inst.comp.clock, BusTargetEvents(inst.engine, inst.comp.bus)
        )
        assert (
            asyncio.run(auditor.audit(inst.comp.kavita_client(target), target)).paused_shelves == []
        )
        with inst.engine.connect() as c:
            shelf_id = c.execute(
                sa.select(export.shelf_destination.c.shelf_id).where(
                    export.shelf_destination.c.id == destination
                )
            ).scalar_one()
        with inst.engine.begin() as c:  # "Famille" is capped at 13+ (L33): raise another shelf
            c.execute(
                sa.update(export.shelf).where(export.shelf.c.id == shelf_id).values(name="Ados")
            )
        ShelfService(inst.engine).set_max_age(shelf_id, 99, actor=None)
        with inst.engine.connect() as c:
            paused = c.execute(
                sa.select(export.shelf.c.paused_reason).where(export.shelf.c.id == shelf_id)
            ).scalar_one()
        assert paused == "child_exposure"  # waiting for a recheck that uses the new ceiling
        (inst.library / "Famille").mkdir(exist_ok=True)
        fam = dataclasses.replace(env, destination_id=destination, shelf_id=shelf_id)
        series = fam.new_series("Never rated")
        fam.add_chapter(series, "1")
        with pytest.raises(DestinationPausedError):
            fam.export()
        assert sorted((inst.library / "Famille").rglob("*.cbz")) == []
        again = asyncio.run(auditor.audit(inst.comp.kavita_client(target), target))
        assert again.exposed and shelf_id not in again.resumed_shelves  # the child of 8 still reads


@pytest.mark.req("ENF-COMP-001", "EF-EXP-055")
@pytest.mark.parametrize("version", ["0.9.0.2", "0.9.0.3", "0.9.0.9", None])
def test_family_export_is_refused_below_the_minimum_or_when_unread(version) -> None:
    """0.9.0.3 is below the 0.9.1 floor that fixes `access_control` (ENF-COMP-001): blocked."""
    with family_env(13, kavita=version) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert env.files() == []


@pytest.mark.req("ENF-COMP-001", "EF-EXP-055")
@pytest.mark.parametrize("version", ["0.9.1.4", "0.9.0.3", "0.9.0.2"])
def test_export_job_reads_the_version_before_writing_for_children(
    version, monkeypatch, settings: Settings, tmp_path: Path
) -> None:
    from shelfhold.catalog.public import RatingService
    from shelfhold.sdk.age import AgeRating
    from tests.l54_env import bench54

    with bench54(monkeypatch, settings, tmp_path) as b:
        dedicated_adult_library(b.env)
        destination = family(b.env)
        link(b.env, destination)
        with b.engine.begin() as c:
            shelf_id = c.execute(
                sa.select(export.shelf_destination.c.shelf_id).where(
                    export.shelf_destination.c.id == destination
                )
            ).scalar_one()
        (b.env.inst.library / "Famille").mkdir(exist_ok=True)
        fam = dataclasses.replace(b.env, destination_id=destination, shelf_id=shelf_id)
        series = fam.new_series("Kids")
        fam.add_chapter(series, "1")
        with b.engine.begin() as c:
            RatingService().rate(c, series.work_id, AgeRating(10), principal=admin(), now=NOW)
            c.execute(sa.update(export.export_target).values(version_seen="0.9.1.4"))  # stale cache
        b.env.inst.kavita.version = version  # the server was downgraded since the last audit
        b.export()
        written = sorted((b.env.inst.library / "Famille").rglob("*.cbz"))
        if version == "0.9.1.4":
            assert len(written) == 1
        else:
            assert written == []
            with b.engine.connect() as c:
                seen = c.execute(sa.select(export.export_target.c.version_seen)).scalar_one()
            assert seen == version  # the probe of the job refreshed what the barrier reads


@pytest.mark.req("EF-BIB-037")
def test_withdrawal_move_failure_is_replayed_at_once_then_at_start() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        rate(env, series, 16)
        service = _withdrawal(env)
        plan = service.plan()
        real = env.lib.fsops.recycle
        calls: list[str] = []

        def flaky(local):
            calls.append(local)
            if len(calls) == 1:
                raise OSError("busy")  # the first move fails, the replay goes through
            return real(local)

        env.lib.fsops.recycle = flaky
        report = service.apply(if_match=plan.version, principal=admin(), now=NOW)
        assert report.files == 1 and report.pending == () and env.files() == []
        assert IntentJournal(env.engine, env.lib.fsops, clock=env.clock).open_intents() == []


@pytest.mark.req("EF-BIB-037")
def test_withdrawal_crash_leaves_an_intent_that_the_next_start_finishes() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        rate(env, series, 16)
        service = _withdrawal(env)
        plan = service.plan()
        real = env.lib.fsops.recycle
        env.lib.fsops.recycle = lambda local: (_ for _ in ()).throw(OSError("crash"))
        report = service.apply(if_match=plan.version, principal=admin(), now=NOW)
        assert len(report.pending) == 1 and len(env.files()) == 1  # rows are settled, file waits
        env.lib.fsops.recycle = real
        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open()
        assert env.files() == []
        assert len(env.pipeline.run(env.destination_id, job_id=new_ulid()).ops) == 0


@pytest.mark.req("EF-EXP-047")
@pytest.mark.parametrize(
    "variant", ["Kavita/", "./Kavita", "Kavita/sub", "LINK", "../manga/Kavita"]
)
def test_two_shelves_never_share_a_folder_by_an_equivalent_path(variant) -> None:
    with family_env(13) as env:
        (env.lib.manga / "LINK").symlink_to(env.lib.manga / "Kavita")
        service = ShelfService(env.engine)
        adult = service.create_shelf(name="Adult", phrase="a", max_age_rating=99)
        with pytest.raises((BindingError, ValueError)):
            service.bind_destination(
                shelf_id=adult, target_id=env.target_id, transport="tree",
                plugin_egress="foyer", root_subpath=variant,
            )  # fmt: skip


@pytest.mark.req("EF-EXP-047")
def test_path_map_change_cannot_put_two_shelves_in_one_folder() -> None:
    """`config set` of a path map replays the overlap rules over the bindings it moves."""
    with family_env(13) as env:
        service = ShelfService(env.engine)
        (env.lib.manga / "Adultes").mkdir(exist_ok=True)
        adult = service.create_shelf(name="Adult", phrase="a", max_age_rating=99)
        with env.engine.begin() as c:
            other = new_ulid()
            c.execute(
                sa.insert(export.export_target).values(
                    id=other, kind="kavita", name="other",
                    path_map=[{"local": str(env.lib.manga / "Adultes"), "remote": "/manga/Adultes",
                               "mode": "rw"}],
                )
            )  # fmt: skip
        service.bind_destination(
            shelf_id=adult, target_id=other, transport="tree", plugin_egress="foyer"
        )
        clash = [{"local": str(env.lib.manga / "Kavita"), "remote": "/manga/Kavita", "mode": "rw"}]
        with pytest.raises(BindingError):  # it would land on the folder of the family shelf
            service.check_path_map(other, clash)


@pytest.mark.req("EF-BIB-037", "EF-USR-015")
def test_cli_shelf_withdraw_shows_the_plan_and_asks_for_its_version(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    from tests.l54_env import bench54

    with bench54(monkeypatch, settings, tmp_path) as b:
        dedicated_adult_library(b.env)
        family(b.env)
        capsys.readouterr()
        assert b.cli("shelf", "withdraw") == 0
        assert "Aucune Édition à retirer" in capsys.readouterr().out


@pytest.mark.req("EF-EXP-055", "EF-USR-015")
def test_pause_set_while_the_export_renders_stops_the_write() -> None:
    """The ceiling is raised (which pauses the linked shelf) and the Work rated 16 while the export
    renders: the pause is read again under the lock, in the reserving transaction."""
    from shelfhold.export.application.target_state import CHILD_EXPOSURE, pause_shelf

    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        real = env.pipeline._renderer.render
        fired: list[int] = []

        def render(path, xml):
            if not fired:
                fired.append(1)
                with env.engine.begin() as connection:
                    connection.execute(
                        sa.update(export.shelf)
                        .where(export.shelf.c.id == env.shelf_id)
                        .values(max_age_rating=99)
                    )
                    pause_shelf(connection, env.shelf_id, CHILD_EXPOSURE)
                rate(env, series, 16)
            return real(path, xml)

        env.pipeline._renderer.render = render
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert fired and env.files() == []
        assert env.rows(export.export_record) == []


@pytest.mark.req("EF-INT-023", "EF-BIB-037")
def test_a_refused_file_waits_in_status_with_its_exit_command() -> None:
    from shelfhold.ops.status import collect

    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        with env.engine.connect() as connection:
            assert [i for i in collect(connection) if i.state == "withdrawal_pending"] == []
        rate(env, series, 16)
        with env.engine.connect() as connection:
            (waiting,) = [i for i in collect(connection) if i.state == "withdrawal_pending"]
        assert waiting.subject == series.edition_id
        assert waiting.command == "shelfhold shelf withdraw" and waiting.reserved
        service = _withdrawal(env)
        service.apply(if_match=service.plan().version, principal=admin(), now=NOW)
        with env.engine.connect() as connection:
            assert [i for i in collect(connection) if i.state == "withdrawal_pending"] == []
