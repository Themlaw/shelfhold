"""EF-BIB-052, EF-BIB-053, EF-SRC-033: `purge --now`, the M1b side (L17). The order is the safety:
the tombstone first, then the service, the index of /store and the copies, then the rows. The
connected service is a stand-in that counts what it is asked to erase; the disks, the database
and the cipher are real."""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import sqlalchemy as sa
import structlog
import structlog.testing
from shelfhold.ops.backup.report import retained_since

from shelfhold.export.application.purge import PurgeReport
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, coherence, platform, storage
from shelfhold.ops.backup.store import archive_name
from tests.export_env import ExportEnv, export_env
from tests.purge_env import ADMIN, TITLE, PurgeEnv, spread
from tests.purge_m1b_env import (
    FakeService,
    cite,
    connected,
    fetched_ticket,
    index_is_coherent,
    index_units,
    make_m1b_env,
    write_index,
)

pytestmark = pytest.mark.req("EF-BIB-052", "EF-BIB-053")


def run(env: PurgeEnv, unit: str) -> PurgeReport:
    return env.purge.purge_unit(unit, principal=ADMIN, typed=unit, now=env.clock.now())


def run_work(env: PurgeEnv, work: str) -> PurgeReport:
    return env.purge.purge_work(work, principal=ADMIN, typed=work, now=env.clock.now())


def count(env: ExportEnv, table: sa.Table, *where: sa.ColumnElement[bool]) -> int:
    with env.engine.connect() as connection:
        return int(
            connection.execute(
                sa.select(sa.func.count()).select_from(table).where(*where)
            ).scalar_one()
        )


# -- /store, the index, the retained backups ----------------------------------------------------


def backup_folder(env: ExportEnv, stamps: dict[str, tuple[datetime, str]]) -> Path:
    """A backup directory and the runs that wrote its archives: `stamp -> (when, state)`."""
    folder = env.lib.tmp / "backups"
    folder.mkdir()
    with env.engine.begin() as connection:
        for stamp, (when, state) in stamps.items():
            name = archive_name(stamp)
            if state == "ok" and not stamp.startswith("2099"):
                (folder / name).write_bytes(b"shelfhold-backup/2 1\n")
            connection.execute(
                sa.insert(platform.backup_run).values(
                    id=new_ulid(), started_at=when, finished_at=when, state=state, file_name=name,
                )
            )  # fmt: skip
    return folder


@pytest.mark.req("EF-BIB-052", "EF-INT-015")
def test_purge_now_removes_store_copy_and_lists_retained_backups() -> None:
    with export_env() as env:
        unit, _ = spread(env, env.new_series(TITLE))
        before = datetime(2000, 1, 1, tzinfo=UTC)
        after = datetime(2100, 1, 1, tzinfo=UTC)
        folder = backup_folder(
            env,
            {
                "20000101T000000Z": (before, "ok"),  # taken before the Unit existed
                "21000101T000000Z": (after, "ok"),  # holds it
                "21000102T000000Z": (after, "failed"),  # never a backup
            },
        )
        # a run whose archive was pruned: nothing to list
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.backup_run).values(
                    id=new_ulid(), started_at=after, finished_at=after, state="ok",
                    file_name=archive_name("21000103T000000Z"),
                )
            )  # fmt: skip
        purge = make_m1b_env(env, backups=lambda since: retained_since(env.engine, folder, since))
        assert (env.lib.store / "units").exists() and write_index(purge)
        report = run(purge, unit)
        assert report.complete
        left = [p for p in env.lib.files_under(env.lib.store) if p.name != "INDEX.jsonl"]
        assert left == []  # the managed copy is gone
        assert [b.name for b in report.retained_backups] == [archive_name("21000101T000000Z")]
        assert "retained_integrated_backups" in report.out_of_reach
        assert sorted(p.name for p in folder.iterdir()) == sorted(  # never rewritten, never erased
            [archive_name("20000101T000000Z"), archive_name("21000101T000000Z")]
        )


@pytest.mark.req("EF-BIB-052", "EF-INT-027")
def test_purge_now_rewrites_store_index() -> None:
    with export_env() as env:
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        other, _ = spread(env, series, "2")
        purge = make_m1b_env(env)
        raw = write_index(purge)
        assert TITLE.encode() in raw and unit in index_units(purge) and other in index_units(purge)
        report = run(purge, unit)
        assert report.complete
        assert index_units(purge) == [other]  # the neighbour stays, the purged Unit is not cited
        assert index_is_coherent(purge)
        assert unit.encode() not in (env.lib.store / "INDEX.jsonl").read_bytes()
        with env.engine.connect() as connection:  # the rewrite was an intent, now closed
            ops = connection.execute(
                sa.select(coherence.fs_intent.c.op, coherence.fs_intent.c.state).where(
                    coherence.fs_intent.c.dst_path == str(env.lib.store / "INDEX.jsonl")
                )
            ).all()
        assert ops and {state for _, state in ops} == {"done"}
        # the last Unit of the index: the file is written empty, not left with its title
        run(purge, other)
        assert index_units(purge) == [] and TITLE not in (env.lib.store / "INDEX.jsonl").read_text()


@pytest.mark.req("EF-BIB-052", "EF-INT-027")
def test_purge_without_an_index_creates_none() -> None:
    with export_env() as env:
        unit, _ = spread(env, env.new_series(TITLE))
        purge = make_m1b_env(env)
        assert not (env.lib.store / "INDEX.jsonl").exists()
        assert run(purge, unit).complete
        assert not (env.lib.store / "INDEX.jsonl").exists()


# -- the connected service ----------------------------------------------------------------------


@pytest.mark.req("EF-BIB-052", "EF-SRC-033")
def test_purge_now_deletes_suwayomi_download_from_m1b(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        unit, _ = spread(env, env.new_series(TITLE))
        request = fetched_ticket(env, service, unit, "ticket-1")
        purge = make_m1b_env(env, service)
        report = run(purge, unit)
        assert report.complete
        assert service.released() == ["ticket-1"]  # one erasure, through the claim release
        assert report.upstream_released == 1 and report.upstream_copies == 1
        assert report.alerts == ()  # nothing is left to delete by hand
        with env.engine.connect() as connection:
            row = connection.execute(
                sa.select(acquisition.download_request).where(
                    acquisition.download_request.c.id == request
                )
            ).one()
        assert row.state == "closed_by_purge" and row.ticket is None and row.variant_id is None
        assert count(env, storage.replica, storage.replica.c.location == "upstream") == 0
        with env.engine.connect() as connection:  # the deed is over, nothing is left open
            open_ones = connection.execute(
                sa.select(coherence.fs_intent.c.id).where(
                    coherence.fs_intent.c.state.in_(("pending", "applying"))
                )
            ).all()
        assert open_ones == []


@pytest.mark.req("EF-SRC-033", "EF-SRC-031")
def test_purge_keeps_the_download_another_account_holds(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path, shared={"ticket-1"})
        unit, _ = spread(env, env.new_series(TITLE))
        fetched_ticket(env, service, unit, "ticket-1")
        report = run(make_m1b_env(env, service), unit)
        assert report.complete and report.upstream_kept_by_others == 1
        assert report.alerts == ("unit.delete_in_suwayomi",)  # measured, never supposed gone


@pytest.mark.req("EF-BIB-052", "EF-SRC-033")
def test_purge_cancels_a_queued_download_and_asks_nothing_more(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        unit, _ = spread(env, env.new_series(TITLE))
        fetched_ticket(env, service, unit, "ticket-q", state="requested")
        (tmp_path / "held.json").write_text("[]")  # queued: the chapter is not downloaded yet
        report = run(make_m1b_env(env, service), unit)
        assert report.complete
        assert service.cancelled() == ["ticket-q"] and service.released() == []


@pytest.mark.req("EF-BIB-052", "EF-SRC-033")
def test_purge_is_incomplete_while_the_service_cannot_be_asked(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        unit, _ = spread(env, env.new_series(TITLE))
        fetched_ticket(env, service, unit, "ticket-1")
        with env.engine.begin() as connection:  # the instance changed: the tokens may name another
            connection.execute(
                sa.insert(acquisition.source_config).values(
                    plugin_id="suwayomi",
                    health={"pause": {"failure_class": "instance", "event_type": "source.x",
                                      "waiting_state": "source_instance_changed",
                                      "since": "2026-11-10T00:00:00+00:00"}},
                )
            )  # fmt: skip
        purge = make_m1b_env(env, service)
        report = run(purge, unit)
        assert not report.complete and service.released() == []
        assert [r.reason for r in report.refused if r.location == "upstream"] == ["service_paused"]
        assert count(env, catalog.unit, catalog.unit.c.id == unit) == 1  # nothing is forgotten
        assert purge.count(coherence.content_tombstone) == 2  # but the tombstone stands
        with env.engine.begin() as connection:
            connection.execute(sa.delete(acquisition.source_config))
        again = run(make_m1b_env(env, service), unit)
        assert again.complete and service.released() == ["ticket-1"]


@pytest.mark.req("EF-BIB-053", "EF-SRC-029")
def test_purge_work_unfollows_suwayomi_series(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        with env.engine.connect() as connection:
            remote = connection.execute(
                sa.select(acquisition.source_item.c.remote_id)
                .select_from(
                    acquisition.source_binding.join(
                        acquisition.source_item,
                        acquisition.source_item.c.id == acquisition.source_binding.c.source_item_id,
                    )
                )
                .where(acquisition.source_binding.c.id == series.binding_id)
            ).scalar_one()
        report = run_work(make_m1b_env(env, service), series.work_id)
        assert report.complete and report.unfollowed == 1
        assert service.unfollowed == [("suwayomi", "suwayomi:1", remote)]
        # the metadata rows of the Work go with it (EF-BIB-053 (4)), and the series it named
        for table, column, value in (
            (catalog.work, catalog.work.c.id, series.work_id),
            (catalog.edition, catalog.edition.c.id, series.edition_id),
            (acquisition.source_binding, acquisition.source_binding.c.id, series.binding_id),
            (catalog.unit, catalog.unit.c.id, unit),
        ):
            assert count(env, table, column == value) == 0
        assert (
            count(env, acquisition.source_item, acquisition.source_item.c.remote_id == remote) == 0
        )
        # the tombstone of the series stays: the follow cycle never takes it up again
        assert purge_tombstones(env) >= 3


def purge_tombstones(env: ExportEnv) -> int:
    return count(env, coherence.content_tombstone)


@pytest.mark.req("EF-BIB-053", "EF-SRC-029")
def test_purge_unit_never_unfollows_the_series(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        report = run(make_m1b_env(env, service), unit)
        assert report.complete and service.unfollowed == [] and report.unfollowed == 0
        assert count(env, catalog.work, catalog.work.c.id == series.work_id) == 1


# -- the rows: jobs, notifications, metadata ----------------------------------------------------


@pytest.mark.req("EF-BIB-053")
def test_purge_removes_mirror_lines_jobs_and_notifications(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        env.add_chapter(series, "2")
        chapter_title = "Hidden chapter"
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.unit).where(catalog.unit.c.id == unit).values(title=chapter_title)
            )
        request = fetched_ticket(env, service, unit, "ticket-1")
        purge = make_m1b_env(env, service)
        marks = cite(purge, request_id=request, unit=unit, title=chapter_title)
        mirrors = sorted((env.lib.library / ".shelfhold" / "manifest").rglob("*.jsonl"))
        assert mirrors and unit in "".join(m.read_text() for m in mirrors)
        report = run(purge, unit)
        assert report.complete
        for path in (env.lib.library / ".shelfhold" / "manifest").rglob("*.jsonl"):
            assert unit not in path.read_text()  # the lines of the Unit are out of the mirror
        jobs = {str(j) for j in purge_rows(env, platform.job_view.c.id)}
        for gone in ("job_request", "job_dedupe", "job_error"):
            assert marks[gone] not in jobs
        assert marks["job_other"] in jobs  # the others are not touched
        pending = purge_rows(env, platform.notification_pending.c.event_id)
        assert marks["note_unit"] not in pending and marks["note_title"] not in pending
        assert marks["note_other"] in pending


def purge_rows(env: ExportEnv, column: sa.Column[object]) -> list[object]:
    with env.engine.connect() as connection:
        return list(connection.execute(sa.select(column)).scalars())


# -- nothing readable is left (EF-BIB-053 (4), the test of M1a extended) -------------------------


@pytest.mark.req("EF-BIB-052", "EF-BIB-053", "EF-INT-027")
def test_purge_leaves_no_readable_title_anywhere(tmp_path: Path) -> None:
    with export_env() as env:
        service = FakeService(tmp_path)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        request = fetched_ticket(env, service, unit, json.dumps({"c": "/manga/dr-stone/ch-1"}))
        purge = make_m1b_env(env, service)
        write_index(purge)
        cite(purge, request_id=request, unit=unit, title=TITLE)
        with structlog.testing.capture_logs() as logged:
            report = run_work(purge, series.work_id)
        assert report.complete
        slug = TITLE.lower().replace(" ", "-")
        needles = (TITLE, TITLE.lower(), slug, "Dr_Stone")
        text = purge.dump() + (
            purge.journal(env.lib.library) + purge.journal(env.lib.store)
        ).decode("latin-1")
        text += (env.lib.store / "INDEX.jsonl").read_text()
        text += json.dumps(logged, default=str, ensure_ascii=False)
        for needle in needles:
            assert needle not in text, needle
        # and in every other row of the database, the tombstones being sealed (EF-BIB-054)
        with env.engine.connect() as connection:
            names = connection.execute(
                sa.text("SELECT name FROM sqlite_master WHERE type = 'table'")
            ).scalars()
            for name in names:
                if name.startswith("sqlite_"):
                    continue
                rows = connection.execute(sa.text(f'SELECT * FROM "{name}"')).all()  # noqa: S608
                blob = json.dumps([[str(c) for c in row] for row in rows], ensure_ascii=False)
                for needle in needles:
                    assert needle not in blob, (name, needle)
        for folder in (env.lib.library, env.lib.store, env.lib.data):
            for path in folder.rglob("*"):
                if path.is_file() and ".shelfhold" not in path.parts:
                    assert TITLE.encode() not in path.read_bytes(), path.name


# -- the migration: what the purges of M1a left upstream ----------------------------------------


@pytest.mark.req("EF-BIB-052", "EF-SRC-033")
def test_m1b_migration_offers_release_of_m1a_purged_upstream_copies(tmp_path: Path) -> None:
    from shelfhold.export.application.purge_leftovers import PurgeLeftovers

    with export_env() as env:
        service = FakeService(tmp_path)
        series = env.new_series(TITLE)
        unit, _ = spread(env, series)
        request = fetched_ticket(env, service, unit, "ticket-m1a")
        gone = fetched_ticket(env, service, env.add_chapter(series, "2"), "ticket-gone")
        (tmp_path / "held.json").write_text(json.dumps(["ticket-m1a"]))  # the other is gone
        m1a = make_m1b_env(env)  # M1a: no service in reach, the claim is withdrawn on our side
        assert run(m1a, unit).complete
        with env.engine.connect() as connection:
            left = connection.execute(
                sa.select(acquisition.download_request.c.ticket).where(
                    acquisition.download_request.c.id == request
                )
            ).scalar_one()
        assert left == "ticket-m1a"  # what M1a left behind

        migration = make_m1b_env(env, service)
        leftovers = PurgeLeftovers(
            env.engine, connected(migration, service), clock=migration.clock, fresh_auth_s=900
        )
        plan = leftovers.plan()  # read only: the service is asked what it holds, never to erase
        assert [i.request_id for i in plan.downloads] == [request]
        assert [i.request_id for i in plan.dead_tickets] == [gone]
        assert service.released() == [] and plan.version
        # nothing is applied by itself: the plan needs the reserved action and its confirmation
        with pytest.raises(PermissionError):
            leftovers.apply(
                if_match=plan.version, principal=KEY_PRINCIPAL, now=migration.clock.now()
            )
        assert service.released() == []
        done = leftovers.apply(if_match=plan.version, principal=ADMIN, now=migration.clock.now())
        assert service.released() == ["ticket-m1a"] and done.released == 1
        assert leftovers.plan().downloads == []  # idempotent: nothing more to offer
        again = leftovers.apply(if_match=plan.version, principal=ADMIN, now=migration.clock.now())
        assert service.released() == ["ticket-m1a"] and again.released == 0


from tests.purge_env import KEY as KEY_PRINCIPAL  # noqa: E402
