"""M3 L22 (EF-BIB-019, EF-EXP-062, EF-BIB-049): merging two Works, separating a wrong merge within
30 days, the redirect of the old identifiers, the effective age and the shelves recomputed, and
the explicit plan that moves the exported series of an Edition. Real database and real schema;
every "never" of the card has a test that tries to break it."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta

import pytest
import sqlalchemy as sa

from shelfhold.catalog.application.rating import RatingService
from shelfhold.catalog.public import (
    MergeAlreadyUndoneError,
    MergeExpiredError,
    StaleMergePlanError,
    WorkMergeRefusedError,
    apply_work_merge,
    plan_work_merge,
    resolve_work,
    separate_works,
)
from shelfhold.export.application.export_items import (
    FrozenSeriesError,
    SeriesReassignmentRefusedError,
    apply_series_reassignment,
    ensure_export_series,
    plan_series_reassignment,
)
from shelfhold.export.domain.planner import SeriesRef
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, identity, platform
from shelfhold.sdk.age import AgeRating
from tests.export_env import ExportEnv
from tests.family_env import NOW, family_env, rate
from tests.integration.test_schema_catalog import add_edition, add_unit

pytestmark = pytest.mark.req("EF-BIB-019", "EF-EXP-062", "EF-BIB-049")

RATING = RatingService()


@pytest.fixture
def env() -> Iterator[ExportEnv]:
    with family_env() as e:
        yield e


def _rate(env: ExportEnv, work_id: str, age: int | None) -> None:
    with env.engine.begin() as c:
        c.execute(
            sa.update(catalog.work)
            .where(catalog.work.c.id == work_id)
            .values(age_rating=age, rated_at=None if age is None else NOW)
        )
        c.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.work_id == work_id)
            .values(age_rating=age, rated_at=None if age is None else NOW)
        )


def _merge(env: ExportEnv, keep: str, drop: str, *, at=NOW):  # type: ignore[no-untyped-def]
    with env.engine.connect() as c:
        plan = plan_work_merge(c, keep, drop)
    with env.engine.begin() as c:
        return apply_work_merge(c, keep, drop, confirm=plan.fingerprint, now=at)


def _work_ids(env: ExportEnv) -> set[str]:
    with env.engine.connect() as c:
        return set(c.execute(sa.select(catalog.work.c.id)).scalars())


def _effective(env: ExportEnv, work_id: str) -> object:
    with env.engine.connect() as c:
        return RATING.effective_age_of(c, work_id)


def _publisher(env: ExportEnv, edition_id: str, name: str) -> None:
    with env.engine.begin() as c:  # two Editions that do not share a key
        c.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == edition_id)
            .values(publisher=name)
        )


def _pair(env: ExportEnv):  # type: ignore[no-untyped-def]
    keep, drop = env.new_series("Alpha Tale"), env.new_series("Alpha Tale (copy)")
    _rate(env, keep.work_id, 0)
    _rate(env, drop.work_id, 16)
    with env.engine.begin() as c:  # two Editions that do not share a key
        c.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == drop.edition_id)
            .values(publisher="Beta")
        )
    return keep, drop


def test_separate_undoes_merge_within_30_days(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    with env.engine.begin() as c:
        unit = add_unit(c, drop.edition_id, "1")
        before = {t.name: c.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
                  for t in (catalog.edition, catalog.unit)}  # fmt: skip
    merged = _merge(env, keep.work_id, drop.work_id)
    assert drop.work_id not in _work_ids(env)  # the absorbed Work is gone
    with env.engine.connect() as c:
        owner = c.execute(
            sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == drop.edition_id)
        ).scalar_one()
    assert owner == keep.work_id
    with env.engine.begin() as c:
        done = separate_works(c, merged.merge_id, now=NOW + timedelta(days=29, hours=23))
    assert done.survivor_id == keep.work_id and done.restored_id == drop.work_id
    assert {keep.work_id, drop.work_id} <= _work_ids(env)  # both Works are back, same ids
    with env.engine.connect() as c:
        rows = dict(c.execute(sa.select(catalog.edition.c.id, catalog.edition.c.work_id)).all())
        assert rows[drop.edition_id] == drop.work_id and rows[keep.edition_id] == keep.work_id
        assert (
            c.execute(
                sa.select(catalog.unit.c.edition_id).where(catalog.unit.c.id == unit)
            ).scalar_one()
            == drop.edition_id
        )
        counts = {t.name: c.execute(sa.select(sa.func.count()).select_from(t)).scalar_one()
                  for t in (catalog.edition, catalog.unit)}  # fmt: skip
        title = c.execute(
            sa.select(catalog.work.c.title).where(catalog.work.c.id == drop.work_id)
        ).scalar_one()
    assert counts == before and title == "Alpha Tale (copy)"


def test_separate_refused_after_30_days_and_twice_and_changes_nothing(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    merged = _merge(env, keep.work_id, drop.work_id)
    with pytest.raises(MergeExpiredError), env.engine.begin() as c:  # the 31st day: it stays
        separate_works(c, merged.merge_id, now=NOW + timedelta(days=30, seconds=1))
    assert drop.work_id not in _work_ids(env)
    with pytest.raises(LookupError), env.engine.begin() as c:
        separate_works(c, new_ulid(), now=NOW)  # an identifier that is no merge
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW + timedelta(days=1))
    with pytest.raises(MergeAlreadyUndoneError), env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW + timedelta(days=1))


def test_work_redirect_after_merge(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    third = env.new_series("Alpha Tale (third)")
    _publisher(env, third.edition_id, "Gamma")
    _rate(env, third.work_id, 0)
    first = _merge(env, drop.work_id, third.work_id)  # third -> drop
    second = _merge(env, keep.work_id, drop.work_id)  # drop -> keep (a chain of two)
    with env.engine.connect() as c:
        assert resolve_work(c, drop.work_id) == keep.work_id
        assert resolve_work(c, third.work_id) == keep.work_id  # the old link is repointed
        assert resolve_work(c, keep.work_id) == keep.work_id
        assert resolve_work(c, "01JNOSUCHWORK00000000000000") == "01JNOSUCHWORK00000000000000"
    with env.engine.begin() as c:
        separate_works(c, second.merge_id, now=NOW)
    with env.engine.connect() as c:
        assert resolve_work(c, drop.work_id) == drop.work_id  # it exists again: no redirect left
        assert resolve_work(c, third.work_id) == drop.work_id  # the chain goes back as it was
    assert first.merge_id != second.merge_id


def test_redirect_never_loops(env: ExportEnv) -> None:
    x, y = env.new_series("Loop X"), env.new_series("Loop Y")
    with env.engine.begin() as c:  # a corrupt table: two live Works that point at each other
        for a, b in ((x.work_id, y.work_id), (y.work_id, x.work_id)):
            c.execute(sa.insert(catalog.work_redirect).values(from_work_id=a, to_work_id=b))
    with env.engine.connect() as c:
        assert resolve_work(c, x.work_id) in {x.work_id, y.work_id}  # it ends


def test_merge_moves_the_user_state_and_separate_gives_it_back(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    user = new_ulid()
    with env.engine.begin() as c:
        c.execute(sa.insert(identity.user).values(**_user_row(user)))
        c.execute(
            sa.insert(identity.user_state).values(user_id=user, work_id=drop.work_id, followed=True)
        )
    merged = _merge(env, keep.work_id, drop.work_id)
    with env.engine.connect() as c:
        rows = c.execute(sa.select(identity.user_state.c.work_id, identity.user_state.c.followed))
        assert [tuple(r) for r in rows] == [(keep.work_id, True)]
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW)
    with env.engine.connect() as c:
        rows = c.execute(sa.select(identity.user_state.c.work_id, identity.user_state.c.followed))
        assert [tuple(r) for r in rows] == [(drop.work_id, True)]


def _user_row(user_id: str) -> dict[str, object]:
    columns = {c.name: c for c in identity.user.columns}
    row: dict[str, object] = {"id": user_id, "username": f"u{user_id[-6:].lower()}"}
    if "role" in columns:
        row["role"] = "member"
    return row


def test_separate_recomputes_effective_age_and_shelves(env: ExportEnv) -> None:
    keep, drop = _pair(env)  # keep 0+, drop 16+, both Editions on the family shelf (13)
    assert str(_effective(env, keep.work_id)) != str(_effective(env, drop.work_id))
    low = _effective(env, keep.work_id)
    merged = _merge(env, keep.work_id, drop.work_id)
    assert merged.effective_age == _effective(env, keep.work_id)  # never below the strictest
    assert merged.effective_age == AgeRating(16)
    # the family shelf no longer admits the Edition of the 16+ Work: reported, never moved by this
    assert drop.edition_id in merged.waiting  # reported with its reason
    with env.engine.connect() as c:
        filed = c.execute(
            sa.select(catalog.edition.c.shelf_id).where(catalog.edition.c.id == drop.edition_id)
        ).scalar_one()
    assert filed == env.shelf_id  # a withdrawal is the admin's (`shelf.withdraw`), never silent
    with env.engine.begin() as c:
        done = separate_works(c, merged.merge_id, now=NOW)
    # a separation never lowers an age: the survivor keeps the 16 the merge gave it
    assert _effective(env, keep.work_id) == AgeRating(16)
    assert _effective(env, drop.work_id) == AgeRating(16)
    assert done.shelves[keep.edition_id] == env.shelf_id  # the 0+ Edition stays on its shelf
    rate(env, keep, 0)  # lowering is the admin's own, audited, rating
    assert _effective(env, keep.work_id) == low


def test_separate_never_lowers_an_age_the_admin_raised_since(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    merged = _merge(env, keep.work_id, drop.work_id)
    rate(env, keep, 18)  # the admin re-rated the merged Work upwards, by the real door
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW)
    assert _effective(env, keep.work_id) == AgeRating(18)
    assert _effective(env, drop.work_id) == AgeRating(18)  # nor the other half


def test_merge_never_lowers_the_age_and_unrated_stays_unrated(env: ExportEnv) -> None:
    keep, drop = env.new_series("Beta Tale"), env.new_series("Beta Tale (copy)")
    _publisher(env, drop.edition_id, "Beta")
    _rate(env, keep.work_id, 18)
    _rate(env, drop.work_id, None)  # nobody rated this one
    _merge(env, keep.work_id, drop.work_id)
    with env.engine.connect() as c:
        rated = c.execute(
            sa.select(catalog.work.c.rated_at).where(catalog.work.c.id == keep.work_id)
        ).scalar_one()
    assert rated is None  # the barrier of EF-BIB-055: one unrated side makes the whole unrated


def test_merge_is_confirmed_by_the_plan_it_was_shown(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    with env.engine.connect() as c:
        plan = plan_work_merge(c, keep.work_id, drop.work_id)
    with env.engine.begin() as c:
        add_edition(c, drop.work_id, lang="en", publisher="Gamma")  # the data moved after the plan
    with pytest.raises(StaleMergePlanError), env.engine.begin() as c:
        apply_work_merge(c, keep.work_id, drop.work_id, confirm=plan.fingerprint, now=NOW)
    with pytest.raises(StaleMergePlanError), env.engine.begin() as c:
        apply_work_merge(c, keep.work_id, drop.work_id, confirm="", now=NOW)
    assert drop.work_id in _work_ids(env)


@pytest.mark.parametrize("case", ["self", "key_collision", "two_series", "other_kind"])
def test_merge_refused_changes_nothing(env: ExportEnv, case: str) -> None:
    keep, drop = _pair(env)
    if case == "key_collision":  # two Editions that would share one key: the Edition plan first
        with env.engine.begin() as c:
            add_edition(c, keep.work_id, lang="en", publisher="Gamma")
            add_edition(c, drop.work_id, lang="en", publisher="Gamma")
    if case == "two_series":
        with env.engine.begin() as c:
            sid, other = new_ulid(), new_ulid()
            for s in (sid, other):
                c.execute(
                    sa.insert(catalog.series).values(
                        id=s, title=f"S {s[-4:]}", sort_title=f"s {s[-4:]}"
                    )
                )
            c.execute(sa.insert(catalog.series_member).values(
                series_id=sid, work_id=keep.work_id, position="1"))  # fmt: skip
            c.execute(sa.insert(catalog.series_member).values(
                series_id=other, work_id=drop.work_id, position="1"))  # fmt: skip
    if case == "other_kind":
        with env.engine.begin() as c:
            kind = c.execute(sa.select(catalog.work.c.kind)).scalars().first()
            c.execute(sa.insert(catalog.work_kind).values(key="other_kind_x", label_fr="x"))
            c.execute(
                sa.update(catalog.work)
                .where(catalog.work.c.id == drop.work_id)
                .values(kind="other_kind_x")
            )
        assert kind
    target = keep.work_id if case == "self" else drop.work_id
    with env.engine.connect() as c:
        if case == "self":
            with pytest.raises(ValueError):
                plan_work_merge(c, keep.work_id, target)
            return
        plan = plan_work_merge(c, keep.work_id, drop.work_id)
    assert plan.blockers and not plan.mergeable
    with pytest.raises(WorkMergeRefusedError), env.engine.begin() as c:
        apply_work_merge(c, keep.work_id, drop.work_id, confirm=plan.fingerprint, now=NOW)
    assert drop.work_id in _work_ids(env)
    with env.engine.connect() as c:
        assert c.execute(sa.select(sa.func.count()).select_from(catalog.merge_log)).scalar() == 0


def test_merge_and_separate_invalidate_the_since_tokens(env: ExportEnv) -> None:
    keep, drop = _pair(env)

    def floor() -> int:
        with env.engine.connect() as c:
            return int(c.execute(sa.select(platform.instance.c.events_purged_through)).scalar_one())

    start = floor()
    merged = _merge(env, keep.work_id, drop.work_id)
    after_merge = floor()
    assert after_merge > start  # a Work was deleted: every token issued before is cut off (L11)
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW)
    assert floor() > after_merge  # the Work that comes back is a Work no client holds either


# -- the exported series: only an explicit plan moves a link (EF-EXP-062, I26) -----------------


def _library_series(env: ExportEnv, edition_id: str, name: str) -> str:
    with env.engine.begin() as c:
        return ensure_export_series(
            c, shelf_destination_id=env.destination_id, remote_library_id=env.remote_library_id,
            ref=SeriesRef(edition_id, name, name.lower()), now=NOW,
        )  # fmt: skip


def _links(env: ExportEnv) -> list[tuple[str, str]]:
    with env.engine.connect() as c:
        link = export.edition_export_series
        return sorted(
            tuple(r) for r in c.execute(sa.select(link.c.edition_id, link.c.export_series_id))
        )


def test_merge_proposes_export_series_reassignment_plan(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    a = _library_series(env, keep.edition_id, "Alpha Tale")
    b = _library_series(env, drop.edition_id, "Alpha Tale (copy)")
    assert a != b
    before = _links(env)
    _merge(env, keep.work_id, drop.work_id)
    assert _links(env) == before  # the merge itself touches no link (I26)
    with env.engine.connect() as c:
        plan = plan_series_reassignment(c, keep.work_id)
    assert [(m.edition_id, m.from_series_id, m.to_series_id) for m in plan.moves] == [
        (drop.edition_id, b, a)
    ]
    assert plan.mergeable and plan.fingerprint
    assert _links(env) == before  # a plan writes nothing
    with pytest.raises(SeriesReassignmentRefusedError), env.engine.begin() as c:
        apply_series_reassignment(c, keep.work_id, confirm="nope", now=NOW)  # no blind apply
    assert _links(env) == before
    with env.engine.begin() as c:
        apply_series_reassignment(c, keep.work_id, confirm=plan.fingerprint, now=NOW)
    assert sorted(s for _e, s in _links(env)) == [a, a]  # one series, both Editions, no duplicate
    with env.engine.connect() as c:
        left = set(c.execute(sa.select(export.export_series.c.id)).scalars())
        again = plan_series_reassignment(c, keep.work_id)
    assert left == {a}  # the emptied series is gone: never an orphan, never a twin
    assert again.moves == ()  # and a second plan has nothing to say


def test_series_reassignment_never_leaves_an_adopted_series(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    a = _library_series(env, keep.edition_id, "Alpha Tale")
    b = _library_series(env, drop.edition_id, "Alpha Tale (copy)")
    with env.engine.begin() as c:  # an adopted series: name and folder are frozen (EF-EXP-052)
        c.execute(
            sa.update(export.export_series)
            .where(export.export_series.c.id == b)
            .values(frozen_at=NOW, frozen_series="Alpha Tale (copy)")
        )
    _merge(env, keep.work_id, drop.work_id)
    with env.engine.connect() as c:
        plan = plan_series_reassignment(c, keep.work_id)
    assert not plan.mergeable and any(x.startswith("frozen") for x in plan.blockers)
    with (
        pytest.raises((SeriesReassignmentRefusedError, FrozenSeriesError)),
        env.engine.begin() as c,
    ):
        apply_series_reassignment(c, keep.work_id, confirm=plan.fingerprint, now=NOW)
    assert dict(_links(env)) == {keep.edition_id: a, drop.edition_id: b}


def test_series_reassignment_warns_when_the_path_changes(
    env: ExportEnv,
) -> None:
    keep, drop = _pair(env)
    _library_series(env, keep.edition_id, "Alpha Tale")
    b = _library_series(env, drop.edition_id, "Alpha Tale (copy)")
    with env.engine.begin() as c:  # files were written under the old series folder
        c.execute(
            sa.update(export.export_series)
            .where(export.export_series.c.id == b)
            .values(series_dir="Alpha Tale (copy)")
        )
    _merge(env, keep.work_id, drop.work_id)
    with env.engine.connect() as c:
        plan = plan_series_reassignment(c, keep.work_id)
    assert plan.path_changes and plan.moves  # the warning of EF-EXP-037: the folder differs
    assert plan.mergeable  # a warning, not a refusal: the files stay where they are
    with env.engine.begin() as c:
        apply_series_reassignment(c, keep.work_id, confirm=plan.fingerprint, now=NOW)
    with env.engine.connect() as c:
        assert not c.execute(
            sa.select(export.export_series.c.id).where(export.export_series.c.id == b)
        ).first()  # the emptied series is gone, no orphan


def test_merge_carries_states_requests_and_fields_and_separate_gives_them_back(
    env: ExportEnv,
) -> None:
    keep, drop = _pair(env)
    both, only = new_ulid(), new_ulid()
    request_id = new_ulid()
    field = catalog.metadata_field
    base = {"provider": "manual", "provenance": "manual", "fetched_at": NOW}
    with env.engine.begin() as c:
        for u in (both, only):
            c.execute(sa.insert(identity.user).values(**_user_row(u)))
        for w, followed, favorite in ((keep.work_id, False, True), (drop.work_id, True, False)):
            c.execute(sa.insert(identity.user_state).values(
                user_id=both, work_id=w, followed=followed, favorite=favorite))  # fmt: skip
        c.execute(
            sa.insert(identity.request).values(
                id=request_id, requester_user_id=only, target_kind="free_text",
                target_value="x", work_id=drop.work_id,
            )
        )  # fmt: skip
        for w, name, value in (
            (drop.work_id, "summary", "from the dropped one"),  # only the dropped Work has it
            (drop.work_id, "genre", "dropped"),  # both have it: the survivor's stays
            (keep.work_id, "genre", "kept"),
        ):
            c.execute(sa.insert(field).values(
                entity_type="work", entity_id=w, field=name, value=value, **base))  # fmt: skip
    merged = _merge(env, keep.work_id, drop.work_id)
    with env.engine.connect() as c:
        flags = c.execute(
            sa.select(identity.user_state.c.followed, identity.user_state.c.favorite)
        ).one()
        assert tuple(flags) == (True, True)  # the union: nothing the reader set is lost
        assert (
            c.execute(
                sa.select(identity.request.c.work_id).where(identity.request.c.id == request_id)
            ).scalar_one()
            == keep.work_id
        )
        values = dict(c.execute(sa.select(field.c.field, field.c.value)).all())
    assert values == {"summary": "from the dropped one", "genre": "kept"}
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW)
    with env.engine.connect() as c:
        states = sorted(
            tuple(r) for r in c.execute(
                sa.select(identity.user_state.c.work_id, identity.user_state.c.followed))
        )  # fmt: skip
        assert states == sorted([(keep.work_id, True), (drop.work_id, True)])
        assert (
            c.execute(
                sa.select(identity.request.c.work_id).where(identity.request.c.id == request_id)
            ).scalar_one()
            == drop.work_id
        )
        rows = {(r.entity_id, r.field): r.value for r in c.execute(sa.select(field))}
    assert rows == {
        (drop.work_id, "summary"): "from the dropped one",
        (drop.work_id, "genre"): "dropped",
        (keep.work_id, "genre"): "kept",
    }


def test_a_half_that_was_unrated_is_unrated_again_after_the_undo(env: ExportEnv) -> None:
    """m3, chosen closed: an admin rating of the merged Work is not a rating of a half that nobody
    rated alone."""
    keep, drop = env.new_series("Gamma Tale"), env.new_series("Gamma Tale (copy)")
    _publisher(env, drop.edition_id, "Beta")
    _rate(env, drop.work_id, 12)
    _rate(env, keep.work_id, None)  # K was never rated
    merged = _merge(env, keep.work_id, drop.work_id)
    rate(env, keep, 12)  # the admin rates the merged Work
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW)
    with env.engine.connect() as c:
        rated = dict(c.execute(sa.select(catalog.work.c.id, catalog.work.c.rated_at)).all())
    assert rated[keep.work_id] is None  # nobody rated K alone
    assert rated[drop.work_id] is not None  # D was rated before and the merged Work is rated now


def test_an_undo_never_settles_a_suggestion_a_merge_opened(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    for work, state in ((keep.work_id, "rejected"), (drop.work_id, "open")):
        with env.engine.begin() as c:
            c.execute(sa.insert(catalog.age_suggestion).values(
                id=new_ulid(), entity_type="work", entity_id=work, value=16, x=False,
                provenance="provider", seen_at=NOW, state=state))  # fmt: skip
    merged = _merge(env, keep.work_id, drop.work_id)

    def states() -> list[tuple[str, str]]:
        with env.engine.connect() as c:
            return sorted(
                tuple(r) for r in c.execute(
                    sa.select(catalog.age_suggestion.c.entity_id, catalog.age_suggestion.c.state))
            )  # fmt: skip

    assert states() == [(keep.work_id, "open")]  # nothing that hid a Work is lost
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW)
    # the survivor's row stays as protective as it is: an undo never settles a suggestion
    assert states() == sorted([(keep.work_id, "open"), (drop.work_id, "open")])


def test_an_edition_with_its_own_age_does_not_raise_the_others(env: ExportEnv) -> None:
    """m1: the Work's ages decide the Work's age; an Edition keeps the one it has."""
    keep, drop = env.new_series("Delta Tale"), env.new_series("Delta Tale (copy)")
    _publisher(env, drop.edition_id, "Beta")
    with env.engine.begin() as c:
        c.execute(sa.update(catalog.work).values(age_rating=6, rated_at=NOW))
        c.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == drop.edition_id)
            .values(age_rating=18, rated_at=NOW)
        )
    _merge(env, keep.work_id, drop.work_id)
    with env.engine.connect() as c:
        own = c.execute(
            sa.select(catalog.work.c.age_rating).where(catalog.work.c.id == keep.work_id)
        ).scalar_one()
    assert own == 6  # the Work itself is not 18+ because one of its Editions is


def test_separation_proposes_a_series_for_the_work_given_back(env: ExportEnv) -> None:
    keep, drop = _pair(env)
    a = _library_series(env, keep.edition_id, "Alpha Tale")
    _library_series(env, drop.edition_id, "Alpha Tale (copy)")
    merged = _merge(env, keep.work_id, drop.work_id)
    with env.engine.connect() as c:
        plan = plan_series_reassignment(c, keep.work_id)
    with env.engine.begin() as c:
        apply_series_reassignment(c, keep.work_id, confirm=plan.fingerprint, now=NOW)
    with env.engine.begin() as c:
        separate_works(c, merged.merge_id, now=NOW)
    with env.engine.connect() as c:
        back = plan_series_reassignment(c, drop.work_id)
        own = plan_series_reassignment(c, keep.work_id)
    assert [(m.edition_id, m.from_series_id, m.to_series_id, m.new_name) for m in back.moves] == [
        (drop.edition_id, a, None, "Alpha Tale (copy)")
    ]
    assert own.moves == ()  # the survivor keeps its own series
    assert _links(env)[0:2] and dict(_links(env))[drop.edition_id] == a  # the plan writes nothing
    with pytest.raises(SeriesReassignmentRefusedError), env.engine.begin() as c:
        apply_series_reassignment(c, drop.work_id, confirm="nope", now=NOW)
    with env.engine.begin() as c:
        apply_series_reassignment(c, drop.work_id, confirm=back.fingerprint, now=NOW)
    links = dict(_links(env))
    assert links[keep.edition_id] == a and links[drop.edition_id] != a
    with env.engine.connect() as c:
        names = sorted(c.execute(sa.select(export.export_series.c.name_raw)).scalars())
        assert plan_series_reassignment(c, drop.work_id).moves == ()
    assert names == ["Alpha Tale", "Alpha Tale (copy)"]


def test_albums_of_one_series_share_its_exported_series_without_a_split(env: ExportEnv) -> None:
    one, two = env.new_series("Epsilon 1"), env.new_series("Epsilon 2")
    sid = new_ulid()
    with env.engine.begin() as c:
        c.execute(sa.insert(catalog.series).values(id=sid, title="E", sort_title="e"))
        for w, pos in ((one.work_id, "1"), (two.work_id, "2")):
            c.execute(
                sa.insert(catalog.series_member).values(series_id=sid, work_id=w, position=pos)
            )
    shared = _library_series(env, one.edition_id, "Epsilon")
    with env.engine.begin() as c:
        ensure_export_series(
            c,
            shelf_destination_id=env.destination_id,
            remote_library_id=env.remote_library_id,
            ref=SeriesRef(two.edition_id, "Epsilon", "epsilon", export_series_id=shared),
            now=NOW,
        )
    with env.engine.connect() as c:
        assert plan_series_reassignment(c, two.work_id).moves == ()
        assert plan_series_reassignment(c, one.work_id).moves == ()
