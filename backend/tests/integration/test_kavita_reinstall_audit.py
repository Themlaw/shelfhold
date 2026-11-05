"""L26 over real files, a real database and the simulated Kavita.

Reinstall found again by path (EF-EXP-039), arrival polling (EF-EXP-034), who owns the fields
(EF-EXP-057) and the read-only access audit (EF-EXP-055).
"""

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.events.waiting_state import WaitingState
from shelfhold.export.application.field_guard import FieldGuard
from shelfhold.export.application.target_state import (
    DestinationPausedError,
    FieldRegistry,
    load_health,
)
from shelfhold.export.domain.field_owner import FieldAction, effective_age
from shelfhold.export.targets.kavita.arrival import ArrivalPoller
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.reinstall import InstanceService
from shelfhold.export.targets.kavita.series import SeriesResolver
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv, export_env, state_of
from tests.fakes.kavita import default_libraries, member
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT, Wired, wire

NEW_LIBRARY = 1  # a reinstalled Kavita hands the ids out again from 1


def _reinstalled_library() -> dict[str, Any]:
    """The admin created the library again on the same folder, with other settings."""
    return {**default_libraries()[1], "id": NEW_LIBRARY, "folders": [REMOTE_ROOT], "name": "Back"}


def _manifest(env: ExportEnv) -> list[tuple[Any, ...]]:
    return [
        (r.export_item_id, r.rel_path, r.state, r.content_hash, r.file_hash)
        for r in env.rows(export.export_record)
    ]


def _health(env: ExportEnv) -> Any:
    with env.engine.connect() as connection:
        return load_health(connection, env.target_id)


def _remote_ref(env: ExportEnv) -> str:
    return str(env.rows(export.remote_library)[0].remote_ref)


# -- EF-EXP-039: a reinstalled Kavita ------------------------------------------------------------


@pytest.mark.req("EF-EXP-039")
def test_links_keyed_by_relative_path() -> None:
    with export_env() as env:
        w = wire(env)
        (rel,) = w.written()
        folder = REMOTE_ROOT + "/" + rel.rsplit("/", 1)[0] if "/" in rel else REMOTE_ROOT
        old = w.fake.add_series("Dr Stone", folder, [w.remote(rel)])
        record = env.rows(export.export_record)[0]
        assert (record.locator_type, record.rel_path) == ("path", rel)  # the key is the path
        with env.engine.begin() as connection, pytest.raises(sa.exc.IntegrityError):
            connection.execute(  # two live records cannot answer to one path
                sa.insert(export.export_record).values(
                    shelf_destination_id=env.destination_id,
                    export_item_id=_other_item(env),
                    locator_type="path",
                    rel_path=rel,
                    state="written",
                )
            )
        w.fake.reinstall("another-install", libraries=[_reinstalled_library()])
        new = w.fake.add_series("Dr Stone", folder, [w.remote(rel)], library_id=NEW_LIBRARY)
        resolution = run(
            SeriesResolver(w.client).resolve(
                library_id=NEW_LIBRARY, remote_folder=folder, cached_id=old
            )
        )
        assert old != new
        assert resolution.by == "path" and resolution.series is not None
        assert resolution.series.id == new  # the cached id is dead, the path still answers


def _other_item(env: ExportEnv) -> str:
    from tests.export_env import ExportEnv as _  # noqa: F401  (typing only)

    with env.engine.begin() as connection:
        from shelfhold.infrastructure.ids import new_ulid

        item_id = new_ulid()
        connection.execute(
            sa.insert(export.export_item).values(id=item_id, kind="unit", frozen_attrs={})
        )
    return item_id


@pytest.mark.req("EF-EXP-039")
def test_kavita_reinstall_reresolves_by_path_without_duplicate_library() -> None:
    with export_env() as env:
        w = wire(env)
        service = InstanceService(env.engine, env.clock, w.events)
        (rel,) = w.written()
        series_id = env.rows(export.export_series)[0].id
        with env.engine.begin() as connection:
            FieldRegistry(connection).record_push(
                env.destination_id, series_id, "age_rating", 18, env.clock.now()
            )
        assert run(service.check(w.client, env.target_id)) is True  # first sight
        before = state_of(env)
        manifest = _manifest(env)

        w.fake.reinstall("another-install", libraries=[_reinstalled_library()])
        w.rebuild_client()
        assert run(service.check(w.client, env.target_id)) is False
        report = run(service.reconnect(w.client, env.target_id))

        assert report.re_resolved == {env.remote_library_id: (str(LIBRARY_ID), str(NEW_LIBRARY))}
        assert report.created == [] and w.fake.requests_to("library/create") == []
        assert len(w.fake.libraries) == 1  # no second library
        assert _remote_ref(env) == str(NEW_LIBRARY)
        assert _manifest(env) == manifest  # not one record rewritten
        assert state_of(env) == before  # not one file touched, same intents, same series
        assert w.fake.violations == []
        # the only write is the settings of the library, applied again
        assert {r.path for r in w.fake.writes} == {"/api/Library/update"}
        assert report.settings_applied == [env.remote_library_id]
        owner = env.rows(export.field_owner)[0]
        assert owner.pushed_at is None and report.invalidated_fields == 1  # to be pushed again
        assert _health(env).instance_changed is False
        with env.engine.connect() as connection:  # the exports may run again
            from shelfhold.export.application.target_state import ensure_active

            ensure_active(connection, env.destination_id)
        assert rel in {r.rel_path for r in env.rows(export.export_record)}


@pytest.mark.req("EF-EXP-039")
def test_kavita_reinstall_creates_only_the_library_that_is_missing() -> None:
    with export_env() as env:
        w = wire(env)
        service = InstanceService(env.engine, env.clock, w.events)
        run(service.check(w.client, env.target_id))
        w.fake.reinstall("blank-install")  # nothing recreated by the admin yet
        w.rebuild_client()
        run(service.reconnect(w.client, env.target_id))
        assert len(w.fake.requests_to("library/create")) == 1
        (library,) = w.fake.libraries
        assert library["folders"] == [REMOTE_ROOT] and library["type"] == 0
        assert library["excludePatterns"] == ["**/.shelfhold/**"]
        assert _remote_ref(env) == str(library["id"])


@pytest.mark.req("EF-EXP-039")
def test_kavita_reinstall_emits_instance_changed_and_pauses() -> None:
    with export_env() as env:
        w = wire(env)
        service = InstanceService(env.engine, env.clock, w.events)
        env.add_chapter(env.main_series(), "2")
        assert run(service.check(w.client, env.target_id)) is True
        assert w.events.codes() == []

        w.fake.reinstall("another-install")
        w.rebuild_client()
        assert run(service.check(w.client, env.target_id)) is False
        assert run(service.check(w.client, env.target_id)) is False
        assert w.events.codes() == ["target.instance_changed"]  # announced once
        health = _health(env)
        assert health.waiting_state is WaitingState.INSTANCE_CHANGED
        with pytest.raises(DestinationPausedError) as paused:
            env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert paused.value.code == "target.instance_changed"
        assert env.files() == []  # nothing was written while waiting for `target reconnect`


# -- EF-EXP-034: arrival polling -----------------------------------------------------------------


def _poller(w: Wired, **options: Any) -> ArrivalPoller:
    return ArrivalPoller(w.env.engine, w.env.clock, w.events, **options)


def _first_scan(w: Wired, rel: str) -> str:
    folder = (REMOTE_ROOT + "/" + rel).rsplit("/", 1)[0]
    run(w.client.scan_folder(folder))
    return folder


@pytest.mark.req("EF-EXP-034")
def test_arrival_confirmed_by_polling_after_the_scan() -> None:
    with export_env() as env:
        w = wire(env)
        (rel,) = w.written()
        folder = (REMOTE_ROOT + "/" + rel).rsplit("/", 1)[0]
        w.fake.index_after_scans("Dr Stone", folder, [w.remote(rel)])
        poller = _poller(w)
        assert run(poller.poll(w.client, env.destination_id)).indexed == []  # not scanned yet
        run(w.client.scan_folder(folder))
        report = run(poller.poll(w.client, env.destination_id))
        assert report.indexed == [rel]
        record = env.rows(export.export_record)[0]
        assert record.state == "resolved" and record.error_code is None
        assert record.remote_ref == str(w.fake.series[0]["id"])  # a cache, from now on
        assert w.events.codes() == []


@pytest.mark.req("EF-EXP-034")
def test_written_but_not_indexed_emits_target_not_indexed() -> None:
    with export_env() as env:
        w = wire(env)
        w.fake.libraries[0]["excludePatterns"] = ["**/Kavita/**"]  # the admin excluded the folder
        w.fake.libraries[0]["folderWatching"] = False
        (rel,) = w.written()
        folder = _first_scan(w, rel)  # the first scan, asked when the file was written
        w.fake.never_indexed.add(folder)
        poller = _poller(w)
        assert run(poller.poll(w.client, env.destination_id)).rescanned == []
        env.clock.advance(minutes=16)  # half the delay: the second scan
        assert run(poller.poll(w.client, env.destination_id)).rescanned == [folder]
        assert w.fake.scans == [folder, folder]  # two scans, no more
        assert env.rows(export.export_record)[0].state == "scanned"
        env.clock.advance(minutes=15)  # the 30 minutes are over
        report = run(poller.poll(w.client, env.destination_id))
        assert report.not_indexed == [rel]
        (detail,) = w.events.details("target.not_indexed")
        assert detail["causes"] == ["excluded_by_pattern", "folder_watching_off"]
        record = env.rows(export.export_record)[0]
        assert record.error_code == "target.not_indexed" and record.state == "scanned"
        assert _health(env).waiting_state is WaitingState.NOT_INDEXED  # never "in progress"
        again = run(poller.poll(w.client, env.destination_id))
        assert again.not_indexed == [] and w.events.codes() == ["target.not_indexed"]  # once
        assert w.fake.scans == [folder, folder] and len(env.files()) == 1  # no re-export


@pytest.mark.req("EF-EXP-034")
def test_written_but_not_indexed_with_the_second_scan_cut() -> None:
    with export_env() as env:
        w = wire(env)
        (rel,) = w.written()
        folder = _first_scan(w, rel)
        w.fake.never_indexed.add(folder)
        poller = _poller(w, max_scans=1)
        env.clock.advance(minutes=20)
        assert run(poller.poll(w.client, env.destination_id)).rescanned == []
        env.clock.advance(minutes=11)
        assert run(poller.poll(w.client, env.destination_id)).not_indexed == [rel]
        assert w.fake.scans == [folder]  # one scan only
        # once the admin fixes the cause, the next poll confirms and leaves the waiting state
        w.fake.never_indexed.clear()
        w.fake.index_after_scans("Dr Stone", folder, [w.remote(rel)])
        run(w.client.scan_folder(folder))
        assert run(poller.poll(w.client, env.destination_id)).indexed == [rel]
        assert env.rows(export.export_record)[0].error_code is None
        assert _health(env).waiting_state is None


# -- EF-EXP-057: Kavita, second writer of metadata -----------------------------------------------


def _series_row(w: Wired, folder: str, name: str = "Dr Stone") -> str:
    return str(w.env.rows(export.export_series)[0].id)


@pytest.mark.req("EF-EXP-057")
def test_series_renamed_in_kavita_no_false_collision() -> None:
    with export_env() as env:
        w = wire(env)
        (rel,) = w.written()
        folder = (REMOTE_ROOT + "/" + rel).rsplit("/", 1)[0]
        mine = w.fake.add_series("Dr Stone (VF)", folder, [w.remote(rel)])  # renamed in the UI
        resolver = SeriesResolver(w.client)
        found = run(resolver.resolve(library_id=LIBRARY_ID, remote_folder=folder, cached_id=mine))
        assert found.by == "cache" and found.renamed("Dr Stone")
        assert found.series is not None and found.series.name == "Dr Stone (VF)"
        collision = run(
            resolver.collision(
                library_id=LIBRARY_ID, planned_name="Dr Stone", remote_folder=folder, own=found
            )
        )
        assert collision is None  # the next chapter goes into the same series
        by_path = run(resolver.resolve(library_id=LIBRARY_ID, remote_folder=folder, cached_id=999))
        assert by_path.by == "path" and by_path.series is not None and by_path.series.id == mine
        assert len(w.fake.series) == 1 and w.fake.writes == []  # no second series, no name written
        # a series that really bears the planned name, elsewhere, is a collision
        other = w.fake.add_series("Dr Stone", "/manga/Other/Dr Stone", ["/manga/Other/x.cbz"])
        clash = run(
            resolver.collision(
                library_id=LIBRARY_ID, planned_name="Dr  stone", remote_folder=folder, own=found
            )
        )
        assert clash is not None and clash.id == other


@pytest.mark.req("EF-EXP-057")
def test_kavita_plus_title_not_overwritten() -> None:
    with export_env() as env:
        w = wire(env)
        w.written()
        series = env.rows(export.export_series)[0].id
        locked = w.fake.add_series("Dr. STONE (Kavita+)", "/manga/Kavita/x", [], name_locked=True)
        info = run(w.client.series_by_ids([locked]))[locked]
        with env.engine.begin() as connection:
            guard = FieldGuard(FieldRegistry(connection), w.events)
            action = guard.decide(
                env.destination_id, series, "title",
                locked_in_kavita=info.name_locked, kavita_value=info.name,
            )  # fmt: skip
            assert (
                action is FieldAction.LEAVE
            )  # a lock that is not in the registry is a third party's
            # the same field, locked by Shelfhold: same value is nothing, another one is drift
            registry = FieldRegistry(connection)
            registry.record_push(env.destination_id, series, "title", info.name, env.clock.now())
            same = guard.decide(
                env.destination_id, series, "title", locked_in_kavita=True, kavita_value=info.name
            )
            assert same is FieldAction.NOTHING and w.events.codes() == []
            edited = guard.decide(
                env.destination_id, series, "title", locked_in_kavita=True, kavita_value="Other"
            )
            assert edited is FieldAction.REPUSH
        (drift,) = w.events.details("reconcile.drift")
        assert drift["cause"] == "field_modified_in_kavita" and drift["field"] == "title"
        assert w.fake.writes == []  # nothing was sent to Kavita


@pytest.mark.req("EF-EXP-057")
def test_kavita_age_from_tags_is_complement_only() -> None:
    with export_env() as env:
        w = wire(env)
        w.written()
        series = env.rows(export.export_series)[0].id
        everyone = w.fake.add_series("A", "/manga/Kavita/a", [], age_rating=3)  # derived from tags
        adults = w.fake.add_series("B", "/manga/Kavita/b", [], age_rating=14, age_locked=False)
        low = run(w.client.series_age(everyone))
        high = run(w.client.series_age(adults))
        assert effective_age(18, low.age_rating) == 18  # the tags never lower a class
        assert effective_age(15, high.age_rating) == 99  # they may raise it
        with env.engine.begin() as connection:
            guard = FieldGuard(FieldRegistry(connection), w.events)
            third = w.fake.add_series("C", "/manga/Kavita/c", [], age_rating=8, age_locked=True)
            locked = run(w.client.series_age(third))
            action = guard.decide(
                env.destination_id, series, "age_rating",
                locked_in_kavita=locked.locked, kavita_value=locked.age_rating,
            )  # fmt: skip
        assert action is FieldAction.LEAVE  # the age of a third party's lock is not rewritten
        assert w.fake.writes == []


# -- EF-EXP-055: the access audit ----------------------------------------------------------------


def _audit(w: Wired) -> AccessAuditor:
    return AccessAuditor(w.env.engine, w.env.clock, w.events)


def _paused(env: ExportEnv) -> str | None:
    reason: str | None = env.rows(export.shelf)[0].paused_reason
    return reason


@pytest.mark.req("EF-EXP-055")
def test_daily_audit_pauses_shelf_on_child_exposure() -> None:
    with export_env() as env:
        w = wire(env)
        auditor = _audit(w)
        assert run(auditor.audit(w.client, env.target_id)).paused_shelves == []
        assert _paused(env) is None and w.events.codes() == []
        audit = env.rows(export.kavita_access_audit)[0]
        assert audit.exposed_to_children is False and audit.accounts == []

        w.fake.users.append(member("kid", libraries=(LIBRARY_ID,), age_rating=8))  # added later
        refused = run(
            auditor.refused_names(w.client, shelf_ceiling=18, remote_library_ref=str(LIBRARY_ID))
        )
        assert refused == ("kid",)  # a link would be refused, naming the account
        report = run(auditor.audit(w.client, env.target_id))  # the daily check
        assert report.paused_shelves == [env.shelf_id] and report.exposed == {"7": ("kid",)}
        assert _paused(env) == "child_exposure"
        (detail,) = w.events.details("target.child_exposure")
        assert detail["accounts"] == ["kid"]
        audit = env.rows(export.kavita_access_audit)[0]
        assert audit.exposed_to_children and audit.age_restriction == 13
        assert audit.accounts[0]["username"] == "kid"
        assert _health(env).waiting_state is WaitingState.CHILD_EXPOSURE
        with pytest.raises(DestinationPausedError) as paused:
            env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert paused.value.code == "target.child_exposure"
        run(auditor.audit(w.client, env.target_id))  # the next day: still paused, no new event
        assert w.events.codes() == ["target.child_exposure"]
        assert w.fake.writes == [] and w.fake.violations == []  # read-only, always
        assert not w.fake.requests_to("/api/Users")[0].headers.get("content-type")

        w.fake.users.pop()  # the account loses its access: only `target recheck` lifts the pause
        assert run(auditor.audit(w.client, env.target_id)).resumed_shelves == []
        assert _paused(env) == "child_exposure"
        assert run(auditor.audit(w.client, env.target_id, resume=True)).resumed_shelves == [
            env.shelf_id
        ]
        assert _paused(env) is None


@pytest.mark.req("EF-EXP-055")
def test_access_control_bad_version_pauses_adult_shelf_when_restricted_account_exists() -> None:
    with export_env() as env:
        w = wire(env, version="0.9.0.2")  # known_bad, category access_control
        w.fake.libraries.append({**w.fake.libraries[0], "id": 8, "folders": ["/other"]})
        auditor = _audit(w)
        none = run(auditor.audit(w.client, env.target_id))
        assert none.paused_shelves == [] and _paused(env) is None  # no restricted account: warn
        assert any("access_control" in warning for warning in none.warnings)

        # restricted to another library: not exposed on its own, but the release is known bad
        w.fake.users.append(member("kid", libraries=(8,)))
        report = run(auditor.audit(w.client, env.target_id))
        assert report.exposed == {} and report.known_bad_restricted == ("kid",)
        assert report.paused_shelves == [env.shelf_id]  # an 18+ shelf, paused all the same
        (detail,) = w.events.details("target.child_exposure")
        assert detail["cause"] == "access_control_known_bad"
        assert _paused(env) == "child_exposure"
        assert w.fake.writes == []

        w.fake.version = "0.9.1.4"  # Kavita updated: `target recheck` is the way out
        recheck = run(auditor.audit(w.client, env.target_id, resume=True))
        assert recheck.resumed_shelves == [env.shelf_id] and _paused(env) is None
