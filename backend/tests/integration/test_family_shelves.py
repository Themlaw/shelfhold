"""L10 (EF-USR-015, EF-BIB-037, EF-BIB-055, EF-EXP-062): the barrier of a family shelf over a real
database, real files and the production pipeline. A Work or an Edition nobody rated, above the
ceiling, new since the last rating or suggested above its rating never reaches a shelf below
18+, by any road: preview, run, a plan stored earlier, a re-export, a series attached late."""

import pytest
import sqlalchemy as sa

from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.withdrawal import WithdrawalRefusedError, WithdrawalService
from shelfhold.identity.public import Credential, Principal
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export
from tests.export_env import ExportEnv
from tests.family_env import (
    NOW,
    add_adult_shelf,
    add_edition_to,
    admin,
    family_env,
    rate,
    rate_edition,
    second_edition,
    suggest,
    unrate,
)


def codes(env: ExportEnv) -> list[str]:
    return sorted({b.code for b in env.pipeline.preview(env.destination_id).blocked})


def run(env: ExportEnv) -> int:
    return len(env.pipeline.run(env.destination_id, job_id=new_ulid()).ops)


def withdrawal(env: ExportEnv) -> WithdrawalService:
    return WithdrawalService(
        env.engine, env.lib.fsops, clock=env.clock, request_scan=env.scans.append
    )


@pytest.mark.req("EF-USR-015", "EF-BIB-055")
def test_unrated_work_blocked_from_non_adult_shelf() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        assert codes(env) == ["shelf.rating_required"]  # rate it first
        assert run(env) == 0 and env.files() == []
        assert env.rows(export.export_record) == []  # not even a reservation
        rate(env, series, 10)  # the admin rates: the way in
        assert codes(env) == []
        assert run(env) == 1 and len(env.files()) == 1


@pytest.mark.req("EF-USR-015")
def test_work_above_shelf_max_blocked() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 16)
        assert codes(env) == ["shelf.above_ceiling"]
        assert run(env) == 0 and env.files() == []
        rate(env, series, 13)  # at the ceiling is under it
        assert run(env) == 1


@pytest.mark.req("EF-BIB-055")
def test_unrated_new_edition_never_enters_family_shelf() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        later = second_edition(env, series)  # arrives after the rating
        env.add_chapter(later, "1")
        assert run(env) == 1  # the rated Edition goes, the new one waits
        assert codes(env) == ["shelf.rating_required"]
        names = {p.parent.name for p in env.files()}
        assert len(names) == 1
        rate_edition(env, later.edition_id, 10)
        assert run(env) == 1 and len(env.files()) == 2


@pytest.mark.req("EF-BIB-055")
def test_higher_suggested_age_blocks_edition() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        suggest(env, series.edition_id, 18, x=True)  # a sub-source says nsfw afterwards
        assert codes(env) == ["shelf.suggestion_above"]
        assert run(env) == 0 and env.files() == []


@pytest.mark.req("EF-BIB-037", "EF-BIB-055")
def test_explicit_edition_age_admits_youth_edition() -> None:
    with family_env(13) as env:
        original = env.new_series()
        env.add_chapter(original, "1")
        youth = second_edition(env, original)
        env.add_chapter(youth, "1")
        rate(env, original, 18)  # the Work is 18+, both Editions rated with it
        rate_edition(env, youth.edition_id, 10)  # an adaptation for children
        assert run(env) == 1
        assert codes(env) == ["shelf.above_ceiling"]  # the 18+ Edition stays out
        (record,) = env.rows(export.export_record)
        with env.engine.connect() as connection:
            edition = connection.execute(
                sa.select(catalog.edition.c.id).select_from(
                    export.export_record.join(
                        export.export_item, export.export_item.c.id == record.export_item_id
                    ).join(catalog.edition, catalog.edition.c.id == export.export_item.c.edition_id)
                )
            ).scalar_one()
        assert edition == youth.edition_id


@pytest.mark.req("EF-BIB-037", "EF-EXP-062")
def test_kavita_series_shelved_by_max_age() -> None:
    """A Work with a youth and an adult Edition: the Kavita series are `Œuvre + Édition`, each on
    its own shelf and in its own library."""
    with family_env(13) as env:
        adult_shelf, adult_destination = add_adult_shelf(env)
        adult = env.new_series("Dr Stone", lang="fr")
        env.add_chapter(adult, "1")
        youth = second_edition(env, adult, lang="en")
        env.add_chapter(youth, "1")
        rate(env, adult, 18)
        rate_edition(env, youth.edition_id, 10)
        with env.engine.begin() as connection:  # the 18+ Edition is filed on the adult shelf
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == adult.edition_id)
                .values(shelf_id=adult_shelf)
            )
        assert run(env) == 1
        again = env.pipeline.run(adult_destination, job_id=new_ulid())
        assert len(again.ops) == 1
        family_names = {p.parent.name for p in env.files()}
        adult_names = {p.parent.name for p in (env.lib.manga / "Adultes").rglob("*.cbz")}
        assert len(family_names) == len(adult_names) == 1
        assert not set(env.files()) & set((env.lib.manga / "Adultes").rglob("*.cbz"))  # two trees
        with env.engine.connect() as connection:
            owners = {
                r.shelf_destination_id for r in connection.execute(sa.select(export.export_series))
            }
        assert len(owners) == 2  # each exported series belongs to a single shelf


@pytest.mark.req("EF-USR-015", "EF-BIB-055")
def test_no_road_writes_for_an_unadmitted_edition() -> None:
    """The planner is one function but the doors are many: a stored plan applied later, a
    re-export, and a plan made while the Edition was admitted."""
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.add_chapter(series, "2")
        rate(env, series, 10)
        preview = env.pipeline.preview(env.destination_id)  # admitted, stored, not yet applied
        assert len(preview.ops) == 2
        suggest(env, series.edition_id, 18)  # the day between the preview and the click
        with pytest.raises(StalePlanError):
            env.pipeline.apply(preview.plan_id, if_match=preview.base_version, job_id=new_ulid())
        assert env.files() == []
        unrate(env, series.edition_id)
        with env.engine.begin() as connection:
            connection.execute(sa.update(catalog.age_suggestion).values(state="rejected"))
        assert run(env) == 0 and env.files() == []  # a new Edition is unrated until rated
        rate(env, series, 10)
        assert run(env) == 2
        with env.engine.begin() as connection:  # re-export (`missing resolve` puts it back)
            connection.execute(sa.update(export.export_record).values(state="planned"))
        unrate(env, series.edition_id)
        before = {p: p.stat().st_ino for p in env.files()}
        assert run(env) == 0
        assert {p: p.stat().st_ino for p in env.files()} == before


@pytest.mark.req("EF-BIB-037", "EF-BIB-055")
def test_age_raising_attach_never_automatic_and_plans_removal() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        env.add_chapter(series, "2")
        rate(env, series, 10)
        assert run(env) == 2 and len(env.files()) == 2
        # an uncensored Edition arrives and the admin rates it 18+: the Work becomes 18+
        uncensored = add_edition_to(env, series)
        rate_edition(env, uncensored, 18)
        service = withdrawal(env)
        plan = service.plan()
        assert [(i.edition_id, i.reason) for i in plan.items] == [
            (series.edition_id, "above_ceiling")
        ]
        assert len(plan.items[0].files) == 2
        assert len(env.files()) == 2  # a plan is shown before anything is applied
        with pytest.raises(WithdrawalRefusedError):  # a key never applies it
            service.apply(if_match=plan.version, principal=_key(), now=NOW)
        assert len(env.files()) == 2
        report = service.apply(if_match=plan.version, principal=admin(), now=NOW)
        assert report.withdrawn == (series.edition_id,)
        assert env.files() == []  # really out of the library of the children
        assert env.scans, "Kavita is asked to scan the folder the files left"
        states = {r.state for r in env.rows(export.export_record)}
        assert states == {"recycled"}
        with env.engine.connect() as connection:
            shelf = connection.execute(
                sa.select(catalog.edition.c.shelf_id).where(
                    catalog.edition.c.id == series.edition_id
                )
            ).scalar_one()
            audited = connection.execute(
                sa.text("SELECT action FROM audit WHERE action = 'shelf.withdraw'")
            ).all()
        assert shelf is None  # "À classer"
        assert len(audited) == 1  # in the transaction that moved the rows
        assert service.plan().items == ()
        assert run(env) == 0  # and nothing writes it back


@pytest.mark.req("EF-BIB-037")
def test_withdrawal_plan_goes_stale_when_the_ratings_move() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        run(env)
        suggest(env, series.edition_id, 18)
        service = withdrawal(env)
        plan = service.plan()
        rate(env, series, 18)  # the admin ruled in the meantime
        with pytest.raises(StalePlanError):
            service.apply(if_match=plan.version, principal=admin(), now=NOW)
        assert len(env.files()) == 1


@pytest.mark.req("EF-BIB-037")
def test_lowering_a_shelf_plans_removal_of_what_no_longer_fits() -> None:
    with family_env(16) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 16)
        run(env)
        with env.engine.begin() as connection:
            connection.execute(sa.update(export.shelf).values(max_age_rating=10))
        plan = withdrawal(env).plan(env.shelf_id)
        assert [i.reason for i in plan.items] == ["above_ceiling"]
        assert len(env.files()) == 1


@pytest.mark.req("EF-USR-015")
def test_a_stored_family_plan_cannot_be_written_when_a_destination_loses_its_partitioning() -> None:
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(export.export_target).values(kind="drop", access_partitioning="none")
            )
        assert codes(env) == ["shelf.destination_not_partitioned"]
        assert run(env) == 0


def _key() -> Principal:
    return Principal(
        user_id=new_ulid(),
        username="admin",
        role="admin",
        credential=Credential.API_KEY,
        key_id=new_ulid(),
        scopes=("admin",),
        can_rate=True,
    )
