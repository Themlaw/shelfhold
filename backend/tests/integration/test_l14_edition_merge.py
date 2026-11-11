"""L14 (EF-BIB-049, EF-BIB-054 (4), EF-BIB-055): merging two Editions of a Work. A plan first,
confirmed by its fingerprint, never a silent recompute; a Unit that would take the place of a
purged one, or a Work whose identifier is tombstoned, stops the merge with nothing deleted; the
age of the survivor is the strictest of the two and the new-Edition barrier holds; two merges that
race leave one winner. Real purge and cipher; the screen is the production adapter."""

import threading
from dataclasses import dataclass

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.catalog.public import (
    EDITION_KEY,
    EditionMergePlan,
    EditionMergeRefusedError,
    ExternalId,
    StaleMergePlanError,
    add_external_id,
    apply_edition_merge,
    plan_after_contributor_merge,
    plan_edition_merge,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, platform
from shelfhold.library.public import EDITION_KEY_COLUMNS, CatalogScreenAdapter, WorkRef
from tests.export_env import ExportEnv, Series
from tests.family_env import NOW, family_env, second_edition
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
)
from tests.purge_env import ADMIN, PurgeEnv, make_purge_env

pytestmark = pytest.mark.req("EF-BIB-049", "EF-BIB-054", "EF-BIB-055")


@dataclass
class Pair:
    env: ExportEnv
    purge: PurgeEnv
    screen: CatalogScreenAdapter
    series: Series  # the survivor (publisher "Alpha"), on the family shelf
    donor: str  # the Edition that goes (publisher "Beta")


def pair(env: ExportEnv) -> Pair:
    purge = make_purge_env(env.lib)
    series = env.new_series("Dr Stone")
    with env.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == series.edition_id)
            .values(publisher="Alpha", age_rating=0, rated_at=NOW)
        )
        donor = add_edition(connection, series.work_id, lang="fr", publisher="Beta", rated_at=NOW)
    return Pair(env, purge, CatalogScreenAdapter(purge.tombstones), series, donor)


def unit(env: ExportEnv, edition_id: str, number: str) -> str:
    with env.engine.begin() as connection:
        return add_unit(connection, edition_id, number)


def plan(p: Pair, keep: str | None = None, drop: str | None = None) -> EditionMergePlan:
    with p.env.engine.connect() as connection:
        return plan_edition_merge(
            connection, keep or p.series.edition_id, drop or p.donor, screen=p.screen
        )


def merge(p: Pair, confirm: str | None = None, keep: str | None = None, drop: str | None = None):
    keep, drop = keep or p.series.edition_id, drop or p.donor
    confirm = confirm or plan(p, keep, drop).fingerprint
    with p.env.engine.begin() as connection:
        return apply_edition_merge(connection, keep, drop, confirm=confirm, screen=p.screen)


def snapshot(env: ExportEnv) -> dict[str, list[tuple[object, ...]]]:
    with env.engine.connect() as connection:
        return {
            t.name: sorted(tuple(map(str, r)) for r in connection.execute(sa.select(t)))
            for t in (catalog.edition, catalog.unit, catalog.volume, acquisition.source_binding)
        }


def edition(env: ExportEnv, edition_id: str) -> sa.Row[tuple[object, ...]] | None:
    with env.engine.connect() as connection:
        return connection.execute(
            sa.select(catalog.edition).where(catalog.edition.c.id == edition_id)
        ).first()


def hits(env: ExportEnv) -> list[object]:
    with env.engine.connect() as connection:
        return [
            r.payload
            for r in connection.execute(
                sa.select(platform.event.c.payload).where(
                    platform.event.c.type == "tombstone.migration_hit"
                )
            )
        ]


def test_the_catalog_and_the_purge_cite_the_same_edition_key() -> None:
    assert tuple(EDITION_KEY) == tuple(EDITION_KEY_COLUMNS)  # a `place` is compared field by field


@pytest.mark.req("EF-BIB-049")
def test_plan_changes_nothing_and_the_merge_needs_its_fingerprint() -> None:
    with family_env(13) as env:
        p = pair(env)
        moved = unit(env, p.donor, "7")
        before = snapshot(env)
        shown = plan(p)
        assert shown.mergeable and shown.units == (moved,) and shown.blockers == ()
        assert snapshot(env) == before  # the plan wrote nothing
        with pytest.raises(StaleMergePlanError):
            merge(p, confirm="0" * 16)
        with pytest.raises(StaleMergePlanError):
            merge(p, confirm=shown.fingerprint[::-1])
        assert snapshot(env) == before  # nothing moved for a wrong confirmation
        done = merge(p, confirm=shown.fingerprint)
        assert done.plan.units == (moved,)
        assert edition(env, p.donor) is None  # the donor is gone, the survivor has its Unit
        with env.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(catalog.unit.c.edition_id).where(catalog.unit.c.id == moved)
                ).scalar()
                == p.series.edition_id
            )


@pytest.mark.req("EF-BIB-049")
def test_a_plan_is_stale_once_the_data_moved() -> None:
    with family_env(13) as env:
        p = pair(env)
        unit(env, p.donor, "7")
        old = plan(p).fingerprint
        unit(env, p.donor, "8")  # the donor changed after the admin read the plan
        with pytest.raises(StaleMergePlanError):
            merge(p, confirm=old)
        assert edition(env, p.donor) is not None


@pytest.mark.req("EF-BIB-054")
def test_identity_migration_consults_tombstones() -> None:
    """Cas fusion d'Éditions: a live Unit of the donor that would land on the place of a purged
    Unit of the survivor blocks the merge. Nothing is deleted or moved; the hit is told once, by
    scope, with no value."""
    with family_env(13) as env:
        p = pair(env)
        purged = env.add_chapter(p.series, "5")
        p.purge.purge.purge_unit(purged, principal=ADMIN, typed=purged, now=p.purge.clock.now())
        live = unit(env, p.donor, "5")  # the donor holds chapter 5, which the survivor purged
        other = unit(env, p.donor, "6")
        before = snapshot(env)
        shown = plan(p)
        assert shown.tombstoned_units == (live,) and not shown.mergeable
        assert other not in shown.tombstoned_units
        with pytest.raises(EditionMergeRefusedError):
            merge(p, confirm=shown.fingerprint)
        assert snapshot(env) == before  # nothing deleted, nothing moved
        p.screen.announce()
        told = hits(env)
        assert told and all(set(h) <= {"scope"} for h in told)  # type: ignore[call-overload]
        assert purged not in str(told) and "Alpha" not in str(told)


@pytest.mark.req("EF-BIB-054")
def test_a_tombstone_on_the_donors_own_key_does_not_block_the_survivor() -> None:
    """The place is compared under the survivor's key, the key the Unit would take."""
    with family_env(13) as env:
        p = pair(env)
        old = unit(env, p.donor, "5")
        p.purge.purge.purge_unit(old, principal=ADMIN, typed=old, now=p.purge.clock.now())
        unit(env, p.donor, "9")
        assert plan(p).mergeable  # (Beta, 5) is a tombstone; (Alpha, 5) is free


@pytest.mark.req("EF-BIB-049", "EF-BIB-054")
def test_a_work_tombstone_blocks_the_merge_of_its_editions() -> None:
    with family_env(13) as env:
        p = pair(env)
        unit(env, p.donor, "7")
        ext = ExternalId("work", "anilist", "30013")
        with env.engine.begin() as connection:
            add_external_id(connection, p.series.work_id, ext, origin="provider")
        p.purge.tombstones.record(WorkRef("anilist", "30013"), p.purge.roots, reason="purge_work")
        before = snapshot(env)
        shown = plan(p)
        assert shown.tombstoned_ids == 1 and not shown.mergeable
        with pytest.raises(EditionMergeRefusedError):
            merge(p, confirm=shown.fingerprint)
        assert snapshot(env) == before


@pytest.mark.req("EF-BIB-049", "EF-BIB-054")
def test_a_merge_without_a_screen_cannot_be_planned() -> None:
    with family_env(13) as env:
        p = pair(env)
        with env.engine.connect() as connection, pytest.raises(TypeError):
            plan_edition_merge(connection, p.series.edition_id, p.donor)  # type: ignore[call-arg]


@pytest.mark.req("EF-BIB-049")
@pytest.mark.parametrize(
    "case",
    ["other_work", "same_place", "same_volume", "same_series", "follow", "export", "relay"],
)
def test_what_the_merge_cannot_carry_refuses_it_and_changes_nothing(case: str) -> None:
    with family_env(13) as env:
        p = pair(env)
        keep, donor = p.series.edition_id, p.donor
        other = env.new_series("Other")
        if case == "export":  # the donor is exported: its files and folders stay where they are
            exported = second_edition(env, p.series)
            donor = exported.edition_id
            env.add_chapter(exported, "1")
            env.pipeline.run(env.destination_id, job_id=new_ulid())
        with env.engine.begin() as connection:
            if case == "other_work":
                donor = other.edition_id
            elif case == "same_place":
                add_unit(connection, keep, "3")
                add_unit(connection, donor, "3")
            elif case == "same_volume":
                for edition_id in (keep, donor):
                    connection.execute(
                        sa.insert(catalog.volume).values(
                            id=new_ulid(), edition_id=edition_id, number="1"
                        )
                    )
            elif case == "same_series":
                item = add_source_item(connection, remote_id="shared")
                add_binding(connection, p.series.work_id, keep, item, priority=2)
                add_binding(
                    connection,
                    p.series.work_id,
                    donor,
                    item,
                    active=False,
                    disabled_reason="manual",
                )
            elif case == "follow":
                connection.execute(
                    sa.insert(acquisition.series_follow).values(
                        edition_id=donor, work_id=p.series.work_id
                    )
                )
            elif case == "relay":
                connection.execute(
                    sa.update(catalog.edition)
                    .where(catalog.edition.c.id == keep)
                    .values(translated_from_edition_id=donor)
                )
        before = snapshot(env)
        shown = plan(p, keep, donor)
        assert not shown.mergeable and shown.blockers
        with pytest.raises(EditionMergeRefusedError):
            merge(p, confirm=shown.fingerprint, keep=keep, drop=donor)
        assert snapshot(env) == before


@pytest.mark.req("EF-BIB-055")
def test_the_survivor_takes_the_strictest_age() -> None:
    with family_env(13) as env:
        p = pair(env)
        unit(env, p.donor, "7")
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == p.donor)
                .values(age_rating=18)
            )
        shown = plan(p)
        assert (shown.keep_age, shown.drop_age, shown.merged_age) == ("0+", "18+", "18+")
        done = merge(p, confirm=shown.fingerprint)
        row = edition(env, p.series.edition_id)
        assert row is not None and row.age_rating == 18  # raised, never lowered
        # and the gate let it go from the family shelf at once, not at the next `shelf withdraw`
        assert row.shelf_id is None and done.filing.shelf_id is None


@pytest.mark.req("EF-BIB-055")
@pytest.mark.parametrize("donor_age", [None, 0, 13])
def test_a_merge_never_lowers_the_age(donor_age: int | None) -> None:
    with family_env(13) as env:
        p = pair(env)
        unit(env, p.donor, "7")
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == p.series.edition_id)
                .values(age_rating=18, age_x=True)
            )
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == p.donor)
                .values(age_rating=donor_age)
            )
        merge(p)
        row = edition(env, p.series.edition_id)
        assert row is not None and (row.age_rating, row.age_x) == (18, True)  # X stays X


@pytest.mark.req("EF-BIB-055")
def test_a_donor_without_an_age_of_its_own_counts_for_the_age_of_its_work() -> None:
    """The adaptation for children (0+) absorbs the Edition that inherits an adult Work's 18+:
    the survivor cannot come out younger than what it took in."""
    with family_env(13) as env:
        p = pair(env)
        unit(env, p.donor, "7")
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.work)
                .where(catalog.work.c.id == p.series.work_id)
                .values(age_rating=18)
            )
        shown = plan(p)
        assert shown.keep_age == "18+" or shown.merged_age == "18+"
        merge(p)
        row = edition(env, p.series.edition_id)
        assert row is not None and row.shelf_id is None  # not on the family shelf any more


@pytest.mark.req("EF-BIB-055")
def test_the_new_edition_barrier_holds_across_a_merge() -> None:
    """A donor nobody rated makes the survivor unrated: its shelf, below 18+, lets it go."""
    with family_env(13) as env:
        p = pair(env)
        unit(env, p.donor, "7")
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == p.donor)
                .values(rated_at=None)
            )
        shown = plan(p)
        assert shown.unrated_after
        merge(p, confirm=shown.fingerprint)
        row = edition(env, p.series.edition_id)
        assert row is not None and row.rated_at is None and row.shelf_id is None


@pytest.mark.req("EF-BIB-055")
def test_a_suggested_age_of_the_donor_follows_its_units() -> None:
    with family_env(13) as env:
        p = pair(env)
        unit(env, p.donor, "7")
        with env.engine.begin() as connection:
            connection.execute(
                sa.insert(catalog.age_suggestion).values(
                    id=new_ulid(),
                    entity_type="edition",
                    entity_id=p.donor,
                    value=18,
                    x=False,
                    provenance="sub_source",
                    seen_at=NOW,
                )
            )
        merge(p)
        with env.engine.connect() as connection:
            owner = connection.execute(sa.select(catalog.age_suggestion.c.entity_id)).scalar()
        assert owner == p.series.edition_id  # still open, still blocking, on the survivor


@pytest.mark.req("EF-BIB-049")
def test_two_merges_that_race_leave_one_winner() -> None:
    with family_env(13) as env:
        p = pair(env)
        a, b = p.series.edition_id, p.donor
        mine, yours = unit(env, a, "1"), unit(env, b, "2")
        fingerprints = {(a, b): plan(p, a, b).fingerprint, (b, a): plan(p, b, a).fingerprint}
        gate = threading.Barrier(2)
        outcomes: dict[tuple[str, str], str] = {}

        def run(engine: Engine, keep: str, drop: str) -> None:
            gate.wait()
            try:
                with engine.connect() as connection:
                    connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                    with connection.begin():
                        apply_edition_merge(
                            connection,
                            keep,
                            drop,
                            confirm=fingerprints[(keep, drop)],
                            screen=CatalogScreenAdapter(p.purge.tombstones),
                        )
                outcomes[(keep, drop)] = "merged"
            except StaleMergePlanError:
                outcomes[(keep, drop)] = "stale"

        threads = [
            threading.Thread(target=run, args=(env.engine, a, b)),
            threading.Thread(target=run, args=(env.engine, b, a)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(outcomes.values()) == ["merged", "stale"]
        survivor = next(k for k, v in outcomes.items() if v == "merged")[0]
        with env.engine.connect() as connection:
            rows = connection.execute(
                sa.select(catalog.unit.c.id, catalog.unit.c.edition_id).where(
                    catalog.unit.c.id.in_([mine, yours])
                )
            ).all()
        assert {r.id for r in rows} == {mine, yours} and {r.edition_id for r in rows} == {survivor}
        assert [e for e in (a, b) if edition(env, e) is not None] == [survivor]


@pytest.mark.req("EF-BIB-049")
def test_contributor_merge_proposes_edition_merge_plan_never_applies() -> None:
    """Two published translations of one book, by two translators; the translators are the same
    person. The merge of the contributors proposes the plan of the two Editions and writes
    nothing: no Edition, Unit or reference moves until an admin confirms the plan."""
    with family_env(13) as env:
        purge = make_purge_env(env.lib)
        screen = CatalogScreenAdapter(purge.tombstones)
        keep_c, drop_c, lone_c = new_ulid(), new_ulid(), new_ulid()
        series = env.new_series("Un roman")
        with env.engine.begin() as connection:
            for cid in (keep_c, drop_c, lone_c):
                connection.execute(
                    sa.insert(catalog.contributor).values(id=cid, kind="person", primary_name=cid)
                )
            ed = {}
            for cid in (keep_c, drop_c, lone_c):
                ed[cid] = add_edition(
                    connection,
                    series.work_id,
                    lang="de" if cid == lone_c else "en",
                    media="text",
                    text_version=f"contributor:{cid}",
                    text_version_contributor_id=cid,
                )
            add_unit(connection, ed[keep_c], "1")
            add_unit(connection, ed[drop_c], "2")
        before = snapshot(env)
        with env.engine.connect() as connection:
            plans = plan_after_contributor_merge(connection, keep_c, drop_c, screen=screen)
        assert [(x.keep_id, x.drop_id) for x in plans] == [(ed[keep_c], ed[drop_c])]
        assert plans[0].mergeable and len(plans[0].units) == 1
        assert snapshot(env) == before  # proposed, never applied
        with (
            env.engine.connect() as connection
        ):  # an Edition of the dropped contributor with no twin
            assert plan_after_contributor_merge(connection, keep_c, lone_c, screen=screen) == ()
        done = merge_with(env, screen, plans[0])  # only the confirmation applies it
        assert done.plan.units == plans[0].units


def merge_with(env: ExportEnv, screen: CatalogScreenAdapter, shown: EditionMergePlan):
    with env.engine.begin() as connection:
        return apply_edition_merge(
            connection,
            shown.keep_id,
            shown.drop_id,
            confirm=shown.fingerprint,
            screen=screen,
        )


@pytest.mark.req("EF-BIB-055")
def test_survivor_without_an_age_of_its_own_takes_the_donors_age() -> None:
    """Review PoC: Work 0+, survivor rated with no explicit age, donor 16+ on the 13 shelf's
    Work. Once the donor is deleted the 16+ must still stand, in the survivor's own age."""
    from shelfhold.catalog.public import RatingService

    with family_env(13) as env:
        p = pair(env)
        with env.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == p.series.edition_id)
                .values(age_rating=None, age_x=False, rated_at=NOW)
            )
            connection.execute(
                sa.update(catalog.work)
                .where(catalog.work.c.id == p.series.work_id)
                .values(age_rating=0)
            )
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == p.donor)
                .values(age_rating=16, rated_at=NOW)
            )
        unit(env, p.donor, "7")
        merge(p)
        row = edition(env, p.series.edition_id)
        assert row is not None and row.age_rating == 16
        with env.engine.connect() as connection:
            verdict = RatingService().admission_of(
                connection, p.series.edition_id, shelf_id=env.shelf_id
            )
        assert verdict.value != "allowed"


@pytest.mark.req("EF-BIB-049")
def test_the_fingerprint_covers_what_follows_the_donor_and_the_fields_move() -> None:
    with family_env(13) as env:
        p = pair(env)
        unit(env, p.donor, "7")
        first = plan(p).fingerprint
        with env.engine.begin() as connection:
            for eid, field, locked in ((p.donor, "summary", True), (p.donor, "title", False)):
                connection.execute(
                    sa.insert(catalog.metadata_field).values(
                        entity_type="edition",
                        entity_id=eid,
                        field=field,
                        lang="und",
                        value="v",
                        provider="manual",
                        provenance="manual",
                        locked=locked,
                        fetched_at=NOW,
                    )
                )
            connection.execute(
                sa.insert(catalog.metadata_field).values(
                    entity_type="edition",
                    entity_id=p.series.edition_id,
                    field="title",
                    lang="und",
                    value="mine",
                    provider="manual",
                    provenance="manual",
                    locked=False,
                    fetched_at=NOW,
                )
            )
        with pytest.raises(StaleMergePlanError):
            merge(p, confirm=first)  # the plan moved: new fields follow the donor
        merge(p)
        with env.engine.connect() as connection:
            rows = {
                (r.field, r.value, r.locked)
                for r in connection.execute(
                    sa.select(catalog.metadata_field).where(
                        catalog.metadata_field.c.entity_type == "edition"
                    )
                )
            }
        assert ("summary", "v", True) in rows  # the lock came along
        assert ("title", "mine", False) in rows and ("title", "v", False) not in rows
