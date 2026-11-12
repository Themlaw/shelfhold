"""Owner decision 2026-11-12: the preview of the adoption of a child library offers a removal plan
for the unrated Works whose files the children can read. The plan is versioned like
`shelf withdraw`, the admin confirms it, and nothing moves without that (so "aucun fichier ne
bouge" still holds for the adoption itself)."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.withdrawal import WithdrawalRefusedError, WithdrawalService
from shelfhold.infrastructure.follow_suspended import suspended_work_ids
from shelfhold.infrastructure.schema import catalog, export
from tests.adopt_env import adoption
from tests.family_env import NOW, admin, family_env
from tests.integration.test_child_adoption import _child_library, _rated_before
from tests.integration.test_family_shelves import _key
from tests.kavita_env import run

pytestmark = pytest.mark.req("EF-EXP-048", "EF-BIB-037")

UNRATED = {
    "Dr Stone": ["Dr Stone/Dr Stone - Ch.1.cbz", "Dr Stone/Dr Stone - Ch.2.cbz"],
    "Monster": ["Monster/Monster - Vol.1.cbz", "Monster/Monster - Vol.2.cbz"],
}


def _service(env, scans):  # type: ignore[no-untyped-def]
    return WithdrawalService(env.engine, env.lib.fsops, clock=env.clock, request_scan=scans.append)


def _preview(a, env):  # type: ignore[no-untyped-def]
    return run(a.adopter.preview(a.wired.client, env.destination_id))


def test_the_preview_offers_a_versioned_plan_and_writes_nothing() -> None:
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        before = a.state()
        plan = _preview(a, env).withdrawal_plan
        assert {s.name: list(s.files) for s in plan.series} == UNRATED
        assert plan.version and plan.version == _preview(a, env).withdrawal_plan.version
        assert a.state() == before  # nothing moved, nothing recycled
        with env.engine.connect() as connection:
            assert suspended_work_ids(connection) == frozenset()


def test_no_plan_for_a_library_the_children_cannot_read() -> None:
    with family_env(13) as env:
        a = adoption(env)  # no restricted account: not a child library
        a.add_series("Dr Stone", ["Dr Stone - Ch.1.cbz"])
        report = _preview(a, env)
        assert not report.child_library
        assert report.withdrawal_plan.series == ()


def test_adopting_moves_no_file_even_with_a_plan_offered() -> None:
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        before = a.state()
        a.adopt()
        assert a.state() == before  # the plan is only offered: nothing without confirmation
        assert _service(env, []).adopted_unrated_plan(env.destination_id).items


def test_the_plan_read_after_adoption_has_the_version_of_the_preview() -> None:
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        seen = _preview(a, env).withdrawal_plan
        a.adopt()
        plan = _service(env, []).adopted_unrated_plan(env.destination_id)
        assert plan.version == seen.version
        assert sorted(f.rel_path for i in plan.items for f in i.files) == sorted(
            f for series in seen.series for f in series.files
        )


def test_confirmed_plan_takes_the_unrated_files_out_and_only_them() -> None:
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        seen = _preview(a, env).withdrawal_plan
        a.adopt()
        scans: list[str] = []
        service = _service(env, scans)
        report = service.apply(
            if_match=seen.version, principal=admin(), now=NOW, destination_id=env.destination_id
        )
        assert report.files == 4 and len(report.withdrawn) == 2
        left = {p.name for p in a.env.lib.tmp.rglob("*.cbz") if ".shelfhold" not in p.parts}
        assert left == {"Akira - Ch.1.cbz", "Lupin - Ch.1.cbz"}  # the rated ones stay
        assert scans, "Kavita is asked to scan the folders the files left"
        with env.engine.connect() as connection:
            audited = connection.execute(
                sa.text("SELECT action FROM audit WHERE action = 'shelf.withdraw'")
            ).all()
            shelves = connection.execute(
                sa.select(catalog.edition.c.shelf_id).where(
                    catalog.edition.c.work_id.in_(suspended_work_ids(connection))
                )
            ).scalars()
            assert set(shelves) == {None}
            assert suspended_work_ids(connection)  # still waiting to be rated
        assert len(audited) == 2
        assert {r.state for r in env.rows(export.export_record)} >= {"recycled"}


def test_nothing_moves_without_the_exact_plan_or_an_admin() -> None:
    """Attack: a stale or empty version, a key, a lapsed session: refused, files stay."""
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        seen = _preview(a, env).withdrawal_plan
        a.adopt()
        before = a.state()
        service = _service(env, [])
        where = {"destination_id": env.destination_id}
        for bad in ("", "0" * 32, seen.version[:-1] + "x"):
            with pytest.raises(StalePlanError):
                service.apply(if_match=bad, principal=admin(), now=NOW, **where)
        with pytest.raises(WithdrawalRefusedError):
            service.apply(if_match=seen.version, principal=_key(), now=NOW, **where)
        assert a.state() == before


def test_a_rating_that_lands_in_between_makes_the_plan_stale() -> None:
    with family_env(13) as env:
        _rated_before(env)
        a = _child_library(env)
        seen = _preview(a, env).withdrawal_plan
        a.adopt()
        with env.engine.connect() as connection:
            work_id = connection.execute(
                sa.select(catalog.work.c.id).where(catalog.work.c.title == "Monster")
            ).scalar_one()
        from shelfhold.catalog.public import RatingService
        from shelfhold.sdk.age import AgeRating

        with env.engine.begin() as connection:
            RatingService().rate(connection, work_id, AgeRating(10), principal=admin(), now=NOW)
        before = a.state()
        with pytest.raises(StalePlanError):
            _service(env, []).apply(
                if_match=seen.version, principal=admin(), now=NOW,
                destination_id=env.destination_id,
            )  # fmt: skip
        assert a.state() == before  # the rated Work is not taken out on an old reading


def test_a_rated_work_is_never_in_the_plan() -> None:
    with family_env(13) as env:
        known = _rated_before(env)
        a = _child_library(env)
        a.adopt()
        plan = _service(env, []).adopted_unrated_plan(env.destination_id)
        names = {f.rel_path.split("/", 1)[0] for i in plan.items for f in i.files}
        assert names == {"Dr Stone", "Monster"}
        assert known  # Akira and Lupin were rated before the adoption
