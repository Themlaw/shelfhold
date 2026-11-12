"""EF-EXP-049 over real files, a real database and the simulated Kavita.

The type of a Kavita library read again (a change pauses the shelf and renames nothing), the
names revalidated through `parse-bulk` after a major update (reported, never renamed), and the
daily jobs that run both (idempotent, a Kavita that does not answer changes nothing).
"""

from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.events.waiting_state import WaitingState
from shelfhold.export.application.target_state import (
    DestinationPausedError,
    ensure_active,
    load_health,
    pause_shelf,
)
from shelfhold.export.targets.kavita.adopt import BulkParse
from shelfhold.export.targets.kavita.dto import LibraryType
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.library_type import LibraryTypeService
from shelfhold.export.targets.kavita.revalidate import Revalidator
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform
from shelfhold.jobs.daily_checks import library_type_check, major_update_replay
from shelfhold.ops.errors import StillWaitingError
from shelfhold.ops.events import BusTargetEvents
from shelfhold.runtime_jobs import connected_clients
from tests.export_env import ExportEnv, add_second_shelf, export_env, state_of
from tests.fakes.kavita import FakeKavita
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID, Wired, wire
from tests.l41_env import instance
from tests.ops_env import make_ops

LOT_EVENTS = frozenset({"target.library_type_changed", "reconcile.drift"})
COMIC = int(LibraryType.COMIC)
MANGA = int(LibraryType.MANGA)


def _set_type(w: Wired, value: int) -> None:
    next(lib for lib in w.fake.libraries if lib["id"] == LIBRARY_ID)["type"] = value


def _health(env: ExportEnv) -> Any:
    with env.engine.connect() as connection:
        return load_health(connection, env.target_id)


def _shelf(env: ExportEnv, shelf_id: str | None = None) -> Any:
    wanted = shelf_id or env.shelf_id
    return next(r for r in env.rows(export.shelf) if r.id == wanted)


def _service(env: ExportEnv, w: Wired) -> LibraryTypeService:
    return LibraryTypeService(env.engine, env.clock, w.events)


def _revalidator(env: ExportEnv, w: Wired) -> Revalidator:
    return Revalidator(env.engine, env.clock, w.events)


def _announced(w: Wired) -> list[str]:
    """The events of this lot (the client says its own, `target.unreachable` and the like)."""
    return [c for c in w.events.codes() if c in LOT_EVENTS]


def _bulk_calls(w: Wired) -> list[Any]:
    return w.fake.requests_to("parse-bulk")


def _single_parses(w: Wired) -> list[Any]:
    return [r for r in w.fake.requests if r.path.lower().endswith("/plugin/parse")]


def _misreads_chapter_3(fake: FakeKavita) -> None:
    """The parser of the new Kavita reads a volume in the name of chapter 3."""
    original = FakeKavita._parsed

    def misread(name: str) -> dict[str, Any]:
        parsed = original(name)
        if "Ch.3" in name:
            parsed = {**parsed, "minVolumeNumber": 1.0, "maxVolumeNumber": 1.0}
        return parsed

    fake._parsed = misread  # type: ignore[method-assign]


# -- the type of the library, read again ---------------------------------------------------------


@pytest.mark.req("EF-EXP-049")
def test_kavita_library_type_change_detected_pauses_shelf() -> None:
    with export_env() as env:
        w = wire(env, manga_parse_only=False)
        w.written(chapters=("2", "3"))
        before = state_of(env)
        writes = len(w.fake.writes)

        assert run(_service(env, w).check(w.client, env.target_id)).changed == ()  # Manga: same
        assert _announced(w) == []
        assert _shelf(env).paused_reason is None

        _set_type(w, COMIC)  # Manga -> Comic, in Kavita
        report = run(_service(env, w).check(w.client, env.target_id))

        assert report.changed == (env.destination_id,)
        assert _announced(w) == ["target.library_type_changed"]
        assert w.events.details("target.library_type_changed")[0]["destination_id"] == (
            env.destination_id
        )
        assert _shelf(env).paused_reason == "library_type_changed"
        assert _health(env).waiting_state is WaitingState.LIBRARY_TYPE_CHANGED
        with env.engine.connect() as connection, pytest.raises(DestinationPausedError) as paused:
            ensure_active(connection, env.destination_id)
        assert paused.value.code == "target.library_type_changed"
        with pytest.raises(DestinationPausedError):  # no export runs while the shelf waits
            env.pipeline.run(env.destination_id, job_id=new_ulid())
        # nothing is renamed or written: the type stored at the adoption stays, the files stay
        assert env.rows(export.shelf_destination)[0].kavita_library_type == "manga"
        assert state_of(env) == before
        assert len(w.fake.writes) == writes and w.fake.violations == []


@pytest.mark.req("EF-EXP-049")
def test_library_type_change_is_announced_once_and_replays_the_sample_with_the_new_type() -> None:
    with export_env() as env:
        w = wire(env, manga_parse_only=False)
        w.written(chapters=("2", "3"))
        _set_type(w, COMIC)
        run(_service(env, w).check(w.client, env.target_id))
        run(_service(env, w).check(w.client, env.target_id))
        assert _announced(w) == ["target.library_type_changed"]  # a second run says nothing
        calls = _bulk_calls(w)
        assert calls and {c.json()["libraryType"] for c in calls} == {COMIC}  # the new type
        assert _single_parses(w) == []  # never one call per name


@pytest.mark.req("EF-EXP-049")
def test_an_unreadable_library_type_is_never_taken_for_a_change() -> None:
    with export_env() as env:
        w = wire(env)
        w.fake.remove_route("GET", "/api/Library/type")  # an old Kavita has no such route
        report = run(_service(env, w).check(w.client, env.target_id))
        assert report.changed == () and _announced(w) == []
        assert _shelf(env).paused_reason is None


@pytest.mark.req("EF-EXP-049")
def test_kavita_unavailable_during_the_type_check_pauses_nothing() -> None:
    with export_env() as env:
        w = wire(env)
        w.written()
        before = state_of(env)
        w.fake.asleep = True
        with pytest.raises(TargetError):  # the job logs it and tries again tomorrow
            run(_service(env, w).check(w.client, env.target_id))
        assert _announced(w) == [] and _shelf(env).paused_reason is None
        assert _health(env).library_type_changed is False
        assert state_of(env) == before
        w.fake.wake_up()
        _set_type(w, COMIC)  # the change is found at the next check, nothing was lost
        assert run(_service(env, w).check(w.client, env.target_id)).changed


@pytest.mark.req("EF-EXP-049")
def test_a_shelf_paused_for_another_reason_keeps_its_pause() -> None:
    with export_env() as env:
        w = wire(env, manga_parse_only=False)
        with env.engine.begin() as connection:
            pause_shelf(connection, env.shelf_id, "child_exposure")
        _set_type(w, COMIC)
        run(_service(env, w).check(w.client, env.target_id))
        assert _shelf(env).paused_reason == "child_exposure"  # the exposure is not hidden
        assert _health(env).library_type_changed is True


@pytest.mark.req("EF-EXP-049")
def test_only_the_shelf_of_the_changed_library_is_paused() -> None:
    with export_env() as env:
        w = wire(env, manga_parse_only=False)
        other_shelf, other_destination = add_second_shelf(env)
        with env.engine.begin() as connection:  # the second shelf lives in the Comics library
            comics = new_ulid()
            connection.execute(
                sa.insert(export.remote_library).values(
                    id=comics, export_target_id=env.target_id, remote_ref="8"
                )
            )
            connection.execute(
                sa.update(export.shelf_destination_remote_library)
                .where(
                    export.shelf_destination_remote_library.c.shelf_destination_id
                    == other_destination
                )
                .values(remote_library_id=comics)
            )
            connection.execute(
                sa.update(export.shelf_destination)
                .where(export.shelf_destination.c.id == other_destination)
                .values(kavita_library_type="comic")
            )
        _set_type(w, COMIC)  # library 7 changes, library 8 is a Comic library still
        report = run(_service(env, w).check(w.client, env.target_id))
        assert report.changed == (env.destination_id,)
        assert _shelf(env).paused_reason == "library_type_changed"
        assert _shelf(env, other_shelf).paused_reason is None


@pytest.mark.req("EF-EXP-049")
def test_library_type_status_lists_the_pause_with_its_exit_command() -> None:
    with export_env() as env:
        w = wire(env, manga_parse_only=False)
        _set_type(w, COMIC)
        run(_service(env, w).check(w.client, env.target_id))
        ops = make_ops(env, w)
        (item,) = [i for i in ops.status().waiting if i.state == "library_type_changed"]
        assert item.command == f"shelfhold target reconnect {env.target_id}"
        assert item.reason


@pytest.mark.req("EF-EXP-049")
def test_reconnect_resumes_the_shelf_once_the_type_is_corrected() -> None:
    with export_env() as env:
        w = wire(env, manga_parse_only=False)
        w.written()
        before = state_of(env)
        _set_type(w, COMIC)
        run(_service(env, w).check(w.client, env.target_id))
        ops = make_ops(env, w)

        with pytest.raises(StillWaitingError):  # Kavita still says Comic: the shelf stays paused
            ops.target_reconnect(env.target_id)
        assert _shelf(env).paused_reason == "library_type_changed"
        assert _health(env).waiting_state is WaitingState.LIBRARY_TYPE_CHANGED

        _set_type(w, MANGA)  # corrected in Kavita
        ops.target_reconnect(env.target_id)
        assert _shelf(env).paused_reason is None
        assert _health(env).library_type_changed is False
        assert not [i for i in ops.status().waiting if i.state == "library_type_changed"]
        with env.engine.connect() as connection:
            ensure_active(connection, env.destination_id)  # the exports may run again
        assert state_of(env) == before  # resuming moves no file


@pytest.mark.req("EF-EXP-049")
def test_reconnect_never_lifts_a_pause_it_did_not_set() -> None:
    with export_env() as env:
        w = wire(env, manga_parse_only=False)
        _set_type(w, COMIC)
        run(_service(env, w).check(w.client, env.target_id))
        with env.engine.begin() as connection:  # another pause took the shelf in the meantime
            connection.execute(sa.update(export.shelf).values(paused_reason="child_exposure"))
        _set_type(w, MANGA)
        make_ops(env, w).target_reconnect(env.target_id)
        assert _shelf(env).paused_reason == "child_exposure"


# -- the names revalidated after a major update --------------------------------------------------


@pytest.mark.req("EF-EXP-049")
def test_kavita_major_update_replays_parse_sample_report() -> None:
    with export_env() as env:
        w = wire(env)
        rels = w.written(chapters=("2", "3", "4"))
        before = state_of(env)
        service = _revalidator(env, w)

        first = run(service.check(w.client, env.target_id))
        assert first.replayed is False and _bulk_calls(w) == []  # first sight: nothing to compare
        assert _health(env).replayed_version == "0.9.1.4"

        w.fake.version = "0.9.2.0"  # a new minor line, and a parser that now misreads chapter 3
        _misreads_chapter_3(w.fake)
        report = run(service.check(w.client, env.target_id))

        assert report.replayed is True and report.version == "0.9.2.0"
        (rejected,) = report.rejected  # the one name the new parser misreads, and why
        assert rejected.rel_path == next(r for r in rels if "Ch.3" in r)
        assert rejected.code == "export.series_name_yields_volume"
        (drift,) = w.events.details("reconcile.drift")
        assert drift["kind"] == "kavita_major_update" and drift["count"] == 1
        assert drift["target_id"] == env.target_id
        assert _health(env).replayed_version == "0.9.2.0"
        assert _health(env).replay_rejected == [rejected.rel_path]
        # reported, never renamed: no write to Kavita, no file, record or intent moved
        assert state_of(env) == before and w.fake.writes == [] and w.fake.violations == []
        assert sorted(r.rel_path for r in env.rows(export.export_record)) == sorted(rels)


@pytest.mark.req("EF-EXP-049")
def test_a_clean_replay_reports_an_empty_drift() -> None:
    with export_env() as env:
        w = wire(env)
        w.written(chapters=("2", "3"))
        run(_revalidator(env, w).check(w.client, env.target_id))
        w.fake.version = "0.9.2.0"
        report = run(_revalidator(env, w).check(w.client, env.target_id))
        assert report.replayed is True and report.rejected == ()
        assert w.events.details("reconcile.drift") == []  # nothing to report, nothing said
        assert _health(env).replay_rejected == []


@pytest.mark.req("EF-EXP-049")
def test_a_patch_release_does_not_replay() -> None:
    with export_env() as env:
        w = wire(env)
        w.written()
        run(_revalidator(env, w).check(w.client, env.target_id))
        w.fake.version = "0.9.1.9"  # same major and minor: the fingerprint did not change
        report = run(_revalidator(env, w).check(w.client, env.target_id))
        assert report.replayed is False and _bulk_calls(w) == []
        assert w.events.details("reconcile.drift") == []


@pytest.mark.req("EF-EXP-049")
def test_the_replay_is_not_lost_when_kavita_does_not_answer() -> None:
    with export_env() as env:
        w = wire(env)
        w.written(chapters=("2", "3"))
        run(_revalidator(env, w).check(w.client, env.target_id))
        w.fake.version = "0.9.2.0"
        _misreads_chapter_3(w.fake)

        w.fake.asleep = True
        with pytest.raises(TargetError):
            run(_revalidator(env, w).check(w.client, env.target_id))
        w.fake.wake_up()
        w.fake.remove_route("POST", "/api/plugin/parse-bulk")  # up, but parse-bulk is gone
        with pytest.raises(TargetError):
            run(_revalidator(env, w).check(w.client, env.target_id))
        assert _health(env).replayed_version == "0.9.1.4"  # still owed
        assert w.events.details("reconcile.drift") == []

        w.fake.missing.clear()
        w.rebuild_client()  # tomorrow's job: a client with no memory of the 404
        report = run(_revalidator(env, w).check(w.client, env.target_id))
        assert report.replayed is True and len(report.rejected) == 1


@pytest.mark.req("EF-EXP-049")
def test_parse_bulk_used_for_replay_and_adoption() -> None:
    with export_env() as env:
        w = wire(env)
        names = [f"Dr Stone - Ch.{n}.cbz" for n in range(250)]
        bulk = run(BulkParse.fetch(w.client, names, LibraryType.MANGA))
        assert len(_bulk_calls(w)) == 3  # 250 names: 100 + 100 + 50, never one call per name
        assert [len(c.json()["names"]) for c in _bulk_calls(w)] == [100, 100, 50]
        assert bulk(names[249]).min_chapter == 249
        assert w.fake.violations == []

        w.written(chapters=tuple(str(n) for n in range(2, 8)))
        w.fake.requests.clear()
        run(_revalidator(env, w).replay_sample(w.client, env.target_id))
        assert _bulk_calls(w)  # the replay goes through the same route
        assert _single_parses(w) == []
        assert all(len(c.json()["names"]) <= 100 for c in _bulk_calls(w))
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-049")
def test_the_replay_asks_only_a_bounded_sample(monkeypatch: pytest.MonkeyPatch) -> None:
    from shelfhold.export.targets.kavita import revalidate

    with export_env() as env:
        w = wire(env)
        w.written(chapters=tuple(str(n) for n in range(2, 12)))
        monkeypatch.setattr(revalidate, "SAMPLE_RECORDS", 3)
        report = run(_revalidator(env, w).replay_sample(w.client, env.target_id))
        assert report.sampled == 3
        asked = {n for c in _bulk_calls(w) for n in c.json()["names"]}
        assert len(asked) <= 3 * 2 + 1  # a file name, its stem, and the series folder


# -- the daily jobs ------------------------------------------------------------------------------


def _clients(env: ExportEnv, *clients: Any) -> Callable[[], list[tuple[str, Any]]]:
    return lambda: [(env.target_id, client) for client in clients]


@pytest.mark.req("EF-EXP-049")
def test_daily_checks_are_idempotent() -> None:
    with export_env() as env:
        w = wire(env, manga_parse_only=False)
        w.written(chapters=("2", "3"))
        type_check = library_type_check(env.engine, env.clock, w.events, _clients(env, w.client))
        replay = major_update_replay(env.engine, env.clock, w.events, _clients(env, w.client))
        replay()  # first sight
        w.fake.version = "0.9.2.0"
        _misreads_chapter_3(w.fake)
        _set_type(w, COMIC)

        for _ in range(3):  # the job runs every day: the same facts, said once
            type_check()
            replay()
        assert _announced(w).count("target.library_type_changed") == 1
        kinds = [d["kind"] for d in w.events.details("reconcile.drift")]
        assert sorted(kinds) == ["kavita_library_type", "kavita_major_update"]  # one each
        replayed = [c for c in _bulk_calls(w) if c.json()["libraryType"] == MANGA]
        assert len(replayed) == 1  # the major update is replayed once
        assert w.fake.writes == [] and w.fake.violations == []


@pytest.mark.req("EF-EXP-049")
def test_a_target_that_fails_does_not_stop_the_daily_checks() -> None:
    with export_env() as env:
        w = wire(env, manga_parse_only=False)
        down = wire_down(env)
        _set_type(w, COMIC)
        type_check = library_type_check(
            env.engine, env.clock, w.events, _clients(env, down.client, w.client)
        )
        type_check()  # the first target does not answer; the job neither raises nor stops
        assert _announced(w) == ["target.library_type_changed"]


def wire_down(env: ExportEnv) -> Wired:
    down = wire(env)
    down.fake.asleep = True
    return down


@pytest.mark.req("EF-EXP-049")
def test_the_daily_jobs_announce_on_the_real_bus_through_the_connected_targets(
    settings: Settings, tmp_path: Any
) -> None:
    with instance(settings, tmp_path) as inst:
        inst.configure()
        inst.kavita.manga_parse_only = False
        with inst.engine.begin() as connection:
            destination = connection.execute(sa.select(export.shelf_destination)).one()
            library_id = new_ulid()
            connection.execute(
                sa.insert(export.remote_library).values(
                    id=library_id, export_target_id=destination.target_id, remote_ref="7"
                )
            )
            connection.execute(
                sa.insert(export.shelf_destination_remote_library).values(
                    shelf_destination_id=destination.id,
                    remote_library_id=library_id,
                    export_target_id=destination.target_id,
                )
            )
        inst.kavita.libraries[0]["type"] = COMIC
        events = BusTargetEvents(inst.engine, inst.comp.bus)
        clients = connected_clients(inst.comp)
        library_type_check(inst.engine, inst.comp.clock, events, clients)()
        library_type_check(inst.engine, inst.comp.clock, events, clients)()

        (shelf,) = inst.engine.connect().execute(sa.select(export.shelf)).all()
        assert shelf.paused_reason == "library_type_changed"
        with inst.engine.connect() as connection:
            rows = connection.execute(
                sa.select(platform.event.c.payload).where(
                    platform.event.c.type == "target.library_type_changed"
                )
            ).all()
        assert [r.payload for r in rows] == [
            {
                "target_id": destination.target_id,
                "destination_id": destination.id,
                "stored_type": MANGA,
                "seen_type": COMIC,
            }
        ]  # the bus keeps the ids and the numbers, once


# -- what the replay leaves aside ----------------------------------------------------------------


@pytest.mark.req("EF-EXP-049")
def test_a_name_that_parse_bulk_does_not_answer_is_rejected_never_assumed_good(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shelfhold.export.targets.kavita import revalidate

    async def silent(_client: Any, _names: Any, _library_type: Any) -> BulkParse:
        return BulkParse({})

    with export_env() as env:
        w = wire(env)
        (rel,) = w.written()
        monkeypatch.setattr(revalidate.BulkParse, "fetch", silent)
        report = run(_revalidator(env, w).replay_sample(w.client, env.target_id))
        assert [(r.rel_path, r.code) for r in report.rejected] == [(rel, revalidate.UNANSWERED)]


@pytest.mark.req("EF-EXP-049")
def test_an_item_that_names_nothing_readable_is_left_out_of_the_sample() -> None:
    with export_env() as env:
        w = wire(env)
        w.written(chapters=("2", "3"))
        with env.engine.begin() as connection:
            item = connection.execute(sa.select(export.export_item.c.id)).scalars().first()
            connection.execute(
                sa.update(export.export_item)
                .where(export.export_item.c.id == item)
                .values(frozen_attrs={"series": "x", "track": "main", "number_norm": "abc"})
            )
        report = run(_revalidator(env, w).replay_sample(w.client, env.target_id))
        assert report.sampled == 1 and report.rejected == ()


@pytest.mark.req("EF-EXP-049")
def test_a_version_that_cannot_be_read_records_nothing() -> None:
    with export_env() as env:
        w = wire(env)
        w.fake.version = ""
        report = run(_revalidator(env, w).check(w.client, env.target_id))
        assert report.replayed is False and report.version is None
        assert _health(env).replayed_version is None


@pytest.mark.req("EF-EXP-049")
def test_the_replay_job_survives_a_kavita_that_does_not_answer() -> None:
    with export_env() as env:
        w = wire(env)
        w.fake.asleep = True
        replay = major_update_replay(env.engine, env.clock, w.events, _clients(env, w.client))
        replay()  # logged, tried again tomorrow
        assert _health(env).replayed_version is None and _announced(w) == []
