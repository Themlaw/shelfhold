"""M4 L19, after the adverse review of #319: the limit of relations, the withdrawal that goes
through a pause, the withdrawal that a death between the call and the note cannot lose, and the
change of audience that asks for it at once."""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import MAX_RELATIONS_PER_WORK
from shelfhold.export.application.shelves import ShelfService
from shelfhold.export.application.target_state import target_write_guard
from shelfhold.export.targets.kavita.related import RelatedSeries
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import export_env
from tests.family_env import add_adult_shelf
from tests.integration.test_child_surfaces import World, api, world  # noqa: F401  (fixtures)
from tests.integration.test_m4_l19_related_push import (
    _library_of,
    indexed,
    posts,
    push,
    relate,
    two,
)
from tests.integration.test_m4_l19_relations import _relate, _twin
from tests.kavita_export_env import wire
from tests.visibility_env import _edition, _work


class _Killed(BaseException):
    """The process dies between Kavita's answer and the end of the pass."""


@pytest.mark.req("EF-BIB-039")
def test_a_work_holds_at_most_the_maximum_of_relations(world: World) -> None:
    hub = _twin(world, "Hub", "ja")
    admin = world.env.client
    for index in range(MAX_RELATIONS_PER_WORK):
        with write_transaction(world.seed.engine) as c:
            kind = str(c.execute(sa.text("SELECT key FROM work_kind LIMIT 1")).scalar_one())
            other = _work(c, kind, f"Satellite {index}", age=6)
            _edition(c, other, lang="ja", shelf_id=world.seed.shelves["adult"])
        assert _relate(world, admin, hub, other, "adaptation_of").status_code == 201
    one_more = world.seed.works["ok"]
    refused = _relate(world, admin, hub, one_more, "adaptation_of")
    assert refused.status_code == 422, refused.text
    assert refused.json()["code"] == "relation.limit"


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
def test_a_withdrawal_goes_through_a_pause_and_an_addition_does_not() -> None:
    with export_env(max_age_rating=None) as env:
        w = wire(env)
        adult_shelf, adult_destination = add_adult_shelf(env)
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(max_age_rating=None, allowed_profiles=[]))
        library = _library_of(env, adult_destination)
        kid, adult = env.new_series("Kid tale"), env.new_series("Grown tale", shelf_id=adult_shelf)
        kk = indexed(w, kid, "Kid tale")
        ka = indexed(w, adult, "Grown tale", destination_id=adult_destination, library_id=library)
        relate(env, adult, kid, "sequel_of")
        assert sorted(push(w)) == sorted([kk, ka])
        # the audience of the adults' shelf narrows and every shelf is paused (as a change of
        # ceiling does): the link that is now forbidden still goes
        with write_transaction(env.engine) as c:
            c.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == adult_shelf)
                .values(allowed_profiles=[new_ulid()])
            )
            c.execute(sa.update(export.shelf).values(paused_reason="child_exposure"))
        w.client.set_write_guard(target_write_guard(env.engine, env.target_id))
        assert sorted(push(w)) == sorted([kk, ka])
        assert w.fake.relations[kk].get("sequels", []) == []
        assert w.fake.relations[ka].get("prequels", []) == []
        # and an addition waits: both open to the same profiles again, still paused
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(allowed_profiles=[]))
        assert push(w) == []
        assert w.fake.relations[kk].get("sequels", []) == []
        before = len(posts(w))
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(paused_reason=None))
        assert sorted(push(w)) == sorted([kk, ka])
        assert len(posts(w)) == before + 2  # one write for each of the two series
        assert w.fake.violations == []


@pytest.mark.req("EF-BIB-039")
def test_a_withdrawal_cut_short_by_a_kill_is_done_again(monkeypatch: pytest.MonkeyPatch) -> None:
    with export_env() as env:
        w = wire(env)
        a, b, ka, _kb = two(w)
        relate(env, b, a, "sequel_of")
        push(w)
        from tests.integration.test_m4_l19_related_push import unrelate

        unrelate(env, b, a, "sequel_of")

        def killed(*_args: Any, **_kwargs: Any) -> None:
            raise _Killed

        with monkeypatch.context() as patch:
            patch.setattr(RelatedSeries, "_record", killed)
            with pytest.raises(_Killed):
                push(w)  # Kavita took the withdrawal, the note did not follow
        assert w.fake.relations[ka].get("sequels", []) == []
        push(w)  # the next pass finds the note as it was and settles it
        assert push(w) == []  # and then nothing is due
        assert w.fake.violations == []


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
def test_a_change_of_audience_asks_for_the_relations_pass() -> None:
    with export_env() as env:
        asked: list[str] = []
        service = ShelfService(env.engine, on_audience_change=lambda: asked.append("now"))
        service.set_allowed_profiles(env.shelf_id, [new_ulid()])
        assert asked == ["now"]
        service.set_max_age(env.shelf_id, 10)
        assert asked == ["now", "now"]
        service.set_max_age(env.shelf_id, 10)  # the same ceiling: nothing changed
        assert asked == ["now", "now"]
