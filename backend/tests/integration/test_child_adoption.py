"""L11 (EF-EXP-048, EF-BIB-036, EF-BIB-055): adopting a library the children can read.

The refusal of M1a is lifted. Nothing moves, nothing is rewritten and nothing is hidden; every Work
nobody rated waits in "À classer" with its following suspended, one Work at a time, and the rest
of the shelf goes on. The files already there stay where they are (the specification says "aucun
fichier ne bouge"): `status` keeps saying that they can be read, and a Work nobody rated never
gets a new file into the shelf of the children. An age read from a ComicInfo is a suggestion.
"""

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import Admission, RatingService
from shelfhold.export.targets.kavita.adopt import AdoptionRefused
from shelfhold.infrastructure.follow_suspended import suspended_work_ids
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, export, identity
from shelfhold.sdk.age import AgeRating
from tests.adopt_env import Adoption, adoption, comicinfo
from tests.export_env import ExportEnv, Series, export_env
from tests.fakes.kavita import member
from tests.family_env import NOW, add_edition_to, admin, family_env, rate
from tests.kavita_env import run
from tests.kavita_export_env import LIBRARY_ID
from tests.ops_env import make_ops

pytestmark = pytest.mark.req("EF-EXP-048")

KID = 8  # a Kavita `AgeRating` that stands for 13+: a child account of the right tier


def _titles(env: ExportEnv, work_ids: frozenset[str]) -> set[str]:
    with env.engine.connect() as connection:
        rows = connection.execute(
            sa.select(catalog.work.c.title).where(catalog.work.c.id.in_(work_ids))
        )
        return {r.title for r in rows}


def _suspended(env: ExportEnv) -> frozenset[str]:
    with env.engine.connect() as connection:
        return suspended_work_ids(connection)


def _library(a: Adoption) -> None:
    """Four series; "Akira" and "Lupin" are Works an admin already rated."""
    a.add_series(
        "Dr Stone", ["Dr Stone - Ch.1.cbz", "Dr Stone - Ch.2.cbz"],
        info=comicinfo("Dr Stone", age="Everyone"),
    )  # fmt: skip
    a.add_series("Monster", ["Monster - Vol.1.cbz", "Monster - Vol.2.cbz"])
    a.add_series("Akira", ["Akira - Ch.1.cbz"])
    a.add_series("Lupin", ["Lupin - Ch.1.cbz"])


def _child_library(env: ExportEnv) -> Adoption:
    a = adoption(env)
    lib = a.wired.fake.libraries[0]
    a.wired.fake.libraries[:] = [{**lib, "folders": ["/manga/Kavita"]}]  # dedicated to the shelf
    a.wired.fake.users.append(member("kid", libraries=(LIBRARY_ID,), age_rating=KID))
    _library(a)
    return a


def _rated_before(env: ExportEnv) -> dict[str, Series]:
    known = {t: env.new_series(t) for t in ("Akira", "Lupin")}
    for series in known.values():
        rate(env, series, 10)
    return known


def test_adopt_heterogeneous_child_library_suspends_unrated_only() -> None:
    with family_env(13) as env:
        known = _rated_before(env)
        a = _child_library(env)
        before = a.state()
        preview = run(a.adopter.preview(a.wired.client, env.destination_id))
        assert preview.child_library and preview.suspended == ("Dr Stone", "Monster")
        assert _suspended(env) == frozenset()  # a preview writes nothing
        report = a.adopt()
        assert report.suspended == ("Dr Stone", "Monster")  # type: ignore[attr-defined]
        assert a.state() == before  # no file moved, no byte, no mtime, no inode
        assert [(r.method, r.path.lower()) for r in a.wired.fake.writes] == [
            ("POST", "/api/library/update")
        ]  # Kavita: only the hidden folder pattern; no scan, nothing hidden or excluded
        assert a.wired.fake.scans == []
        suspended = _suspended(env)
        assert _titles(env, suspended) == {"Dr Stone", "Monster"}  # the unrated ones, by Work
        assert known["Akira"].work_id not in suspended  # rated: keeps being followed
        assert known["Lupin"].work_id not in suspended
        with env.engine.connect() as connection:
            listed = connection.execute(sa.select(export.shelf.c.follow_suspended)).scalars()
            assert sorted(w for ws in listed for w in ws) == sorted(suspended)
            assert connection.execute(sa.select(export.shelf.c.paused_reason)).scalar() is None
        ops = make_ops(env, a.wired)
        waiting = {i.subject: i for i in ops.status().waiting if i.state == "follow_suspended"}
        assert set(waiting) == set(suspended)
        assert all(i.command.startswith("shelfhold rate ") for i in waiting.values())
        # the files that are there stay readable: `status` says so, nothing is taken out by itself
        held = {i.subject for i in ops.status().waiting if i.state == "withdrawal_pending"}
        assert len(held) == 2


def test_adopted_comicinfo_age_is_suggestion_only() -> None:
    with family_env(13) as env:
        a = _child_library(env)
        a.adopt()
        with env.engine.connect() as connection:
            work = connection.execute(
                sa.select(catalog.work).where(catalog.work.c.title == "Dr Stone")
            ).one()
            edition = connection.execute(
                sa.select(catalog.edition).where(catalog.edition.c.work_id == work.id)
            ).one()
            (suggestion,) = connection.execute(
                sa.select(catalog.age_suggestion).where(
                    catalog.age_suggestion.c.entity_id == edition.id
                )
            )
            verdict = RatingService().admission_of(connection, edition.id, shelf_id=env.shelf_id)
        assert (suggestion.value, suggestion.x, suggestion.provenance) == (0, False, "comicinfo")
        assert work.age_rating is None and work.rated_at is None  # not a rating
        assert edition.age_rating is None and edition.rated_at is None
        assert verdict is Admission.UNRATED_EDITION  # "Everyone" never admits it to the family


def test_no_unrated_adopted_file_is_made_readable() -> None:
    """The proof: whatever arrives after the adoption for an unrated Work stays out of the shelf
    of the children, and the Works that were rated are exported as before."""
    with family_env(13) as env:
        known = _rated_before(env)
        a = _child_library(env)
        a.adopt()
        with env.engine.connect() as connection:
            adopted = {
                r.title: (r.work_id, r.id)
                for r in connection.execute(
                    sa.select(catalog.work.c.title, catalog.edition.c.id, catalog.edition.c.work_id)
                    .select_from(catalog.edition.join(catalog.work))
                    .where(catalog.work.c.title.in_(("Dr Stone", "Monster")))
                )
            }
        assert set(adopted) == {"Dr Stone", "Monster"}
        for work_id, edition_id in adopted.values():
            # the follow cycle never asks for a suspended Work, but a Unit that exists anyway
            # (a download made before, a manual import) must not go out
            env.add_chapter(_bind(env, work_id, edition_id), "9")
        for series in known.values():
            env.add_chapter(series, "9")
        there = {p.name for p in env.files()}
        assert {b.code for b in env.pipeline.preview(env.destination_id).blocked} == {
            "shelf.rating_required"
        }
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        new = {p.name for p in env.files()} - there
        assert new == {"Akira - Ch.9.cbz", "Lupin - Ch.9.cbz"}  # the rated ones, nothing else
        # an admin rates "Monster": it enters; "Dr Stone" still waits
        rate(env, _bind(env, *adopted["Monster"]), 10)
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        later = {p.name for p in env.files()} - there - new
        assert len(later) == 1 and next(iter(later)).startswith("Monster")


def _bind(env: ExportEnv, work_id: str, edition_id: str) -> Series:
    from tests.integration.test_schema_catalog import add_binding, add_source_item

    table = acquisition.source_binding
    with env.engine.begin() as connection:
        found = connection.execute(
            sa.select(table.c.id).where(table.c.edition_id == edition_id)
        ).scalar()
        if found is None:
            item = add_source_item(connection, remote_id=f"series-{edition_id}")
            found = add_binding(connection, work_id, edition_id, item)
    return Series(work_id, edition_id, str(found))


def test_rating_a_suspended_work_lifts_its_suspension() -> None:
    with family_env(13) as env:
        a = _child_library(env)
        a.adopt()
        (stone,) = [w for w in _suspended(env) if _titles(env, frozenset({w})) == {"Dr Stone"}]
        with env.engine.begin() as connection:
            RatingService().rate(connection, stone, AgeRating(10), principal=admin(), now=NOW)
        assert _titles(env, _suspended(env)) == {"Monster", "Akira", "Lupin"}  # this Work alone
        with env.engine.connect() as connection:
            actions = connection.execute(sa.select(identity.audit.c.action)).scalars().all()
        assert actions.count("rate") == 1


def test_adult_shelf_exposed_to_children_is_adopted_not_refused() -> None:
    """M1a refused it (`test_child_library_adoption_refused_before_m1b`): now it is adopted, the
    unrated Works wait, and the exposure of the shelf stays what the audit says (paused)."""
    with export_env() as env:
        a = adoption(env)
        a.wired.fake.users.append(member("enfant", libraries=(LIBRARY_ID,), age_rating=6))
        _library(a)
        before = a.state()
        report = a.adopt()
        assert report.exposed_accounts == ("enfant",)  # type: ignore[attr-defined]
        assert a.state() == before
        assert len(_suspended(env)) == 4  # nothing was rated: every Work waits
        with env.engine.connect() as connection:
            paused = connection.execute(sa.select(export.shelf.c.paused_reason)).scalar()
        assert paused == "child_exposure"  # EF-EXP-055: an adult shelf the children read stops


def test_family_shelf_refuses_accounts_below_its_ceiling() -> None:
    with family_env(13) as env:
        a = adoption(env)
        a.wired.fake.users.append(member("tout-petit", libraries=(LIBRARY_ID,), age_rating=5))
        _library(a)
        with pytest.raises(AdoptionRefused) as refused:
            run(a.adopter.preview(a.wired.client, env.destination_id))
        assert refused.value.accounts == ("tout-petit",)


def test_second_edition_of_an_adopted_work_stays_unrated() -> None:
    with family_env(13) as env:
        a = _child_library(env)
        a.adopt()
        with env.engine.connect() as connection:
            work = connection.execute(
                sa.select(catalog.work.c.id).where(catalog.work.c.title == "Monster")
            ).scalar_one()
        edition = add_edition_to(env, Series(work, "", ""), lang="en")
        with env.engine.connect() as connection:
            verdict = RatingService().admission_of(connection, edition, shelf_id=env.shelf_id)
        assert verdict is Admission.UNRATED_EDITION
