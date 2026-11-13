"""L13 (EF-EXP-040, EF-EXP-039, EF-EXP-057): the age of a series pushed by the Kavita API, locked,
for the fields Shelfhold owns only; a change of shelf goes through the removal plan of L10; no
exported file is rewritten for a change of metadata. The pipeline, the database and the files are
the production ones; Kavita is the simulated server (no network)."""

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.target_state import FieldRegistry
from shelfhold.export.application.withdrawal import WithdrawalService
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.polling import ArrivalPoller
from shelfhold.export.targets.kavita.reinstall import InstanceService
from shelfhold.export.targets.kavita.series import SeriesAges
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.export_env import ExportEnv
from tests.fakes.kavita import default_libraries
from tests.family_env import family_env, rate
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT, Wired, wire

TEEN, EVERYONE_10, MATURE_17, ADULTS, X_RATING = 8, 5, 10, 13, 14  # Kavita's `AgeRating`


def index(w: Wired, title: str = "Dr Stone", **options: Any) -> int:
    rels = sorted(r.rel_path for r in w.env.rows(export.export_record))
    folder = (REMOTE_ROOT + "/" + rels[0]).rsplit("/", 1)[0]
    sid = w.fake.add_series(title, folder, [w.remote(r) for r in rels], **options)
    poller = ArrivalPoller(w.env.engine, w.env.clock, w.events)
    run(poller.poll(w.client, w.env.destination_id))
    return sid


def push(w: Wired, **options: Any) -> list[int]:
    ages = SeriesAges(w.env.engine, w.env.clock, w.events)
    pushed: list[int] = run(ages.push(w.client, w.env.destination_id, **options))
    return pushed


def metadata_posts(w: Wired) -> list[Any]:
    return [r for r in w.fake.requests_to("/Series/metadata") if r.method == "POST"]


def hashes(env: ExportEnv) -> list[tuple[str, str | None]]:
    return sorted((r.rel_path, r.file_hash) for r in env.rows(export.export_record))


def registered(env: ExportEnv) -> Any:
    with env.engine.connect() as connection:
        return FieldRegistry(connection).get(
            env.destination_id, env.rows(export.export_series)[0].id, "age_rating"
        )


@pytest.mark.req("EF-EXP-040")
def test_reclassify_same_shelf_pushes_age_via_api() -> None:
    with family_env(13) as env:
        w = wire(env)
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        sid = index(w)
        before = hashes(env)

        assert push(w) == [sid]
        assert w.fake.ages[sid]["ageRating"] == EVERYONE_10
        assert w.fake.ages[sid]["ageRatingLocked"] is True
        assert registered(env).last_value == EVERYONE_10 and registered(env).pushed

        rate(env, series, 13)  # the same shelf: 13+ is its ceiling
        assert push(w) == [sid]
        assert w.fake.ages[sid]["ageRating"] == TEEN and w.fake.ages[sid]["ageRatingLocked"]
        assert registered(env).last_value == TEEN
        assert push(w) == []  # nothing changed since: nothing sent
        assert len(metadata_posts(w)) == 2
        assert w.fake.violations == []
        assert hashes(env) == before  # the file never moved


@pytest.mark.req("EF-EXP-040", "EF-EXP-046")
def test_reclassify_out_of_family_shelf_plans_removal_never_an_age_push() -> None:
    with family_env(13) as env:
        w = wire(env)
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        sid = index(w)
        assert push(w) == [sid]
        posts = len(metadata_posts(w))

        rate(env, series, 16)  # above the 13+ ceiling: the Work must leave the shelf
        assert push(w) == []  # no age is pushed over the library of the children
        assert len(metadata_posts(w)) == posts
        assert w.fake.ages[sid]["ageRating"] == EVERYONE_10
        plan = WithdrawalService(
            env.engine, env.lib.fsops, clock=env.clock, request_scan=env.scans.append
        ).plan()
        assert [i.edition_id for i in plan.items] == [series.edition_id]  # L10's plan, not a push


@pytest.mark.req("EF-EXP-040")
def test_metadata_change_does_not_rewrite_exported_file() -> None:
    with family_env(13) as env:
        w = wire(env)
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        sid = index(w)
        before, files = hashes(env), env.files()
        stat = [(f, f.stat().st_mtime_ns, f.read_bytes()) for f in files]

        with env.engine.begin() as connection:  # a corrected title, a new sort title
            connection.execute(
                sa.update(catalog.unit).where(catalog.unit.c.edition_id == series.edition_id)
                .values(title="Another title")
            )  # fmt: skip
            connection.execute(
                sa.update(catalog.work).where(catalog.work.c.id == series.work_id)
                .values(sort_title="zzz")
            )  # fmt: skip
        rate(env, series, 13)
        report = env.pipeline.run(env.destination_id, job_id=new_ulid())
        assert not report.ops  # no file operation is planned for a metadata change
        assert push(w) == [sid]  # the one thing that moves is the age, by the API
        assert hashes(env) == before and env.files() == files
        assert [(f, f.stat().st_mtime_ns, f.read_bytes()) for f in files] == stat


@pytest.mark.req("EF-EXP-057")
def test_kavita_field_rewritten_only_if_owned_by_shelfhold() -> None:
    with family_env(13) as env:
        w = wire(env)
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        # the admin locked the summary and the genres in Kavita; the age is free
        sid = index(w, name_locked=True)
        w.fake.ages[sid].update(summary="Mine", summaryLocked=True, genres=[{"title": "x"}])
        w.fake.ages[sid]["genresLocked"] = True
        assert push(w) == [sid]
        document = w.fake.ages[sid]
        assert document["ageRating"] == EVERYONE_10 and document["ageRatingLocked"] is True
        assert (document["summary"], document["summaryLocked"]) == ("Mine", True)
        assert document["genres"] == [{"title": "x"}] and document["genresLocked"] is True
        assert w.fake.series[0]["name"] == "Dr Stone" and w.fake.series[0]["nameLocked"]

        rate(env, series, 13)  # reclassified: only the age moves
        assert push(w) == [sid]
        document = w.fake.ages[sid]
        assert document["ageRating"] == TEEN and document["summary"] == "Mine"
        assert w.events.codes() == []

        # the age locked by the admin himself (the registry knows nothing of it) is never touched
        with env.engine.begin() as connection:
            connection.execute(sa.delete(export.field_owner))
        w.fake.ages[sid].update(ageRating=ADULTS, ageRatingLocked=True)
        rate(env, series, 10)
        assert push(w) == []
        assert w.fake.ages[sid]["ageRating"] == ADULTS
        assert w.fake.violations == []


@pytest.mark.req("EF-EXP-057", "ENF-OPS-011")
def test_modified_field_in_kavita_emits_drift_and_is_pushed_again() -> None:
    with family_env(13) as env:
        w = wire(env)
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        sid = index(w)
        assert push(w) == [sid]
        w.fake.ages[sid]["ageRating"] = MATURE_17  # Shelfhold's own lock, another value
        assert push(w) == [sid]
        (drift,) = w.events.details("reconcile.drift")
        assert drift["cause"] == "field_modified_in_kavita" and drift["field"] == "age_rating"
        assert w.events.emitted[0][0] == "reconcile.drift"
        assert w.fake.ages[sid]["ageRating"] == EVERYONE_10  # pushed again, not abandoned


@pytest.mark.req("EF-EXP-057")
def test_age_of_tags_raises_the_pushed_age_never_lowers_it() -> None:
    with family_env(13) as env:
        w = wire(env)
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        sid = index(w, age_rating=X_RATING)  # Kavita derived "adults only" from the tags
        assert push(w) == [sid]
        assert w.fake.ages[sid]["ageRating"] == X_RATING  # the tags are heeded, never lowered
        assert w.fake.ages[sid]["ageRatingLocked"] is True


def _reinstalled_library() -> dict[str, Any]:
    return {**default_libraries()[1], "id": 1, "folders": [REMOTE_ROOT], "name": "Back"}


@pytest.mark.req("EF-EXP-039", "EF-EXP-057")
def test_kavita_reconnect_repushes_rated_series_ages() -> None:
    with family_env(13) as env:
        w = wire(env)
        rated = env.new_series("Rated")
        env.add_chapter(rated, "1")
        rate(env, rated, 13)
        taken = env.new_series("Taken")
        env.add_chapter(taken, "1")
        rate(env, taken, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        rels = sorted(r.rel_path for r in env.rows(export.export_record))
        for title in ("Rated", "Taken"):
            rel = next(r for r in rels if r.startswith(title))
            w.fake.add_series(
                title, f"{REMOTE_ROOT}/{title}", [w.remote(rel)], age_rating=0, age_locked=False
            )
        run(ArrivalPoller(env.engine, env.clock, w.events).poll(w.client, env.destination_id))
        assert len(push(w)) == 2
        service = InstanceService(env.engine, env.clock, w.events)
        run(service.check(w.client, env.target_id))

        # Kavita is reinstalled: the series come back with no age; the admin locked one by hand
        w.fake.reinstall("another-install", libraries=[_reinstalled_library()])
        w.rebuild_client()
        assert run(service.check(w.client, env.target_id)) is False
        new = {
            title: w.fake.add_series(
                title, f"{REMOTE_ROOT}/{title}",
                [w.remote(next(r for r in rels if r.startswith(title)))], library_id=1,
            )
            for title in ("Rated", "Taken")
        }  # fmt: skip
        w.fake.ages[new["Taken"]].update(ageRating=ADULTS, ageRatingLocked=True)
        before = len(metadata_posts(w))
        auditor = AccessAuditor(env.engine, env.clock, w.events)
        report = run(service.reconnect(w.client, env.target_id, auditor=auditor))
        assert report.ages_pushed == [new["Rated"]]
        assert w.fake.ages[new["Rated"]]["ageRating"] == TEEN
        assert w.fake.ages[new["Rated"]]["ageRatingLocked"] is True
        assert w.fake.ages[new["Taken"]]["ageRating"] == ADULTS  # a lock Shelfhold does not hold
        assert len(metadata_posts(w)) == before + 1 and w.fake.violations == []
        assert LIBRARY_ID != 1  # the ids really were handed out again


@pytest.mark.req("EF-EXP-040")
def test_unrated_work_pushes_no_age() -> None:
    with family_env(13) as env:
        w = wire(env)
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        index(w)
        with env.engine.begin() as connection:
            connection.execute(sa.update(catalog.work).values(age_rating=None, age_x=False))
            connection.execute(sa.update(catalog.edition).values(age_rating=None, age_x=False))
        assert push(w) == [] and metadata_posts(w) == []
