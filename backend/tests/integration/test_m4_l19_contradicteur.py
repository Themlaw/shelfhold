"""Relecture adverse de la PR #319 (M4 L19, œuvres liées) : PoC rouges.

Chaque test décrit le comportement attendu. Il échoue aujourd'hui pour la raison donnée dans
`docs/decisions/M4.md`, section « #319 : contradicteur ». Le `xfail(strict=True)` signalera le jour
où il passe.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.targets.kavita.related import RelatedSeries
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import export_env
from tests.family_env import add_adult_shelf
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    api,
    world,
)
from tests.integration.test_m4_l19_related_push import (
    _library_of,
    indexed,
    push,
    relate,
    two,
    unrelate,
)
from tests.integration.test_m4_l19_relations import _merge, _relate, _rows, _twin
from tests.kavita_export_env import wire


class _Killed(BaseException):
    """The process dies (kill -9) between Kavita's answer and the commit of the registry."""


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
@pytest.mark.xfail(strict=True, reason="#319 M1: a push not recorded is never withdrawn")
def test_a_push_killed_before_its_record_is_still_withdrawn_when_audiences_split(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with export_env(max_age_rating=None) as env:
        w = wire(env)
        adult_shelf, adult_destination = add_adult_shelf(env)
        with write_transaction(env.engine) as c:  # both libraries open to everyone, for now
            c.execute(sa.update(export.shelf).values(max_age_rating=None, allowed_profiles=[]))
        library = _library_of(env, adult_destination)
        kid, adult = env.new_series("Kid tale"), env.new_series("Grown tale", shelf_id=adult_shelf)
        kk = indexed(w, kid, "Kid tale")
        ka = indexed(w, adult, "Grown tale", destination_id=adult_destination, library_id=library)
        relate(env, adult, kid, "sequel_of")

        def killed(*_args: Any, **_kwargs: Any) -> None:
            raise _Killed

        with monkeypatch.context() as patch:
            patch.setattr(RelatedSeries, "_record", killed)
            with pytest.raises(_Killed):
                push(w)  # Kavita holds the relation, the registry does not know it
        sequels, prequels = w.fake.relations.get(kk, {}), w.fake.relations.get(ka, {})
        assert ka in sequels.get("sequels", []) or kk in prequels.get("prequels", [])
        # the adults' shelf is now closed to the child: the libraries stop sharing an audience
        with write_transaction(env.engine) as c:
            c.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == adult_shelf)
                .values(allowed_profiles=[new_ulid()])
            )
        push(w)
        assert w.fake.relations.get(kk, {}).get("sequels", []) == []
        assert w.fake.relations.get(ka, {}).get("prequels", []) == []


@pytest.mark.req("EF-BIB-039")
@pytest.mark.xfail(strict=True, reason="#319 m1: a relation a person set is taken as Shelfhold's")
def test_a_relation_a_person_set_before_the_push_stays_when_shelfhold_withdraws() -> None:
    with export_env() as env:
        w = wire(env)
        a, b, ka, kb = two(w)
        w.fake.relations[ka] = {"sequels": [kb]}  # set by hand in Kavita, before any push
        w.fake.relations[kb] = {"prequels": [ka]}
        relate(env, b, a, "sequel_of")  # the admin says the same thing in Shelfhold
        push(w)
        unrelate(env, b, a, "sequel_of")
        push(w)
        # Shelfhold added nothing on these lists: it has nothing to take back
        assert w.fake.relations[ka]["sequels"] == [kb]
        assert w.fake.relations[kb]["prequels"] == [ka]


@pytest.mark.req("EF-BIB-039")
@pytest.mark.xfail(strict=True, reason="#319 m2: a merge closes a loop the API refuses")
def test_a_merge_never_closes_a_loop_of_sequels(world: World) -> None:
    keep, drop = _twin(world, "Tale of Survivor", "ja"), _twin(world, "Tale of Absorbed", "de")
    middle = world.seed.works["ok"]
    admin = world.env.client
    assert _relate(world, admin, keep, middle, "sequel_of").status_code == 201  # keep follows mid
    assert _relate(world, admin, middle, drop, "sequel_of").status_code == 201  # mid follows drop
    _merge(world, keep, drop)
    rows = _rows(world)
    loop = (keep, middle, "sequel_of") in rows and (middle, keep, "sequel_of") in rows
    assert not loop, rows  # the survivor is the sequel of its own sequel
