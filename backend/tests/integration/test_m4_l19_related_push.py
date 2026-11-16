"""M4 L19 (EF-BIB-039, ENF-SEC-010): related Works pushed to the « related series » of Kavita.

The service account (the dedicated admin key of the target) writes the relation on both series, and
only between series of libraries open to the same profiles: a relation never crosses from a library
a child may read to one she may not. What a third party put in Kavita's relations is read and sent
back untouched; what Shelfhold pushed is withdrawn when the relation is removed or when the two
libraries stop being open to the same profiles. The pipeline, the database and the registry are the
production ones; Kavita is the simulated server (no network).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import relate_works, unrelate_works
from shelfhold.export.application.target_state import FieldRegistry, target_write_guard
from shelfhold.export.targets.kavita.related import RELATED_FIELD, RelatedSeries
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv, Series, export_env
from tests.family_env import add_adult_shelf
from tests.kavita_env import run
from tests.kavita_export_env import Wired, wire

NOW = datetime(2026, 11, 16, 10, 0, tzinfo=UTC)


def _library_of(env: ExportEnv, destination_id: str) -> str:
    with env.engine.connect() as c:
        return str(
            c.execute(
                sa.select(export.shelf_destination_remote_library.c.remote_library_id).where(
                    export.shelf_destination_remote_library.c.shelf_destination_id == destination_id
                )
            ).scalar_one()
        )


def indexed(
    w: Wired,
    series: Series,
    name: str,
    *,
    destination_id: str | None = None,
    library_id: str | None = None,
) -> int:
    """The Work's Edition is a series Kavita has indexed (its id is the cache of the series)."""
    env = w.env
    destination_id = destination_id or env.destination_id
    library_id = library_id or env.remote_library_id
    kavita_id = w.fake.add_series(name, f"/manga/{name}", [f"/manga/{name}/{name} - Ch.001.cbz"])
    series_id = new_ulid()
    with write_transaction(env.engine) as c:
        c.execute(
            sa.insert(export.export_series).values(
                id=series_id,
                shelf_destination_id=destination_id,
                remote_library_id=library_id,
                normalized_name=name.lower(),
                name_raw=name,
                kavita_series_id=str(kavita_id),
            )
        )
        c.execute(
            sa.insert(export.edition_export_series).values(
                edition_id=series.edition_id,
                remote_library_id=library_id,
                export_series_id=series_id,
                assigned_at=NOW,
                assigned_by="first_export",
            )
        )
    return kavita_id


def relate(env: ExportEnv, a: Series, b: Series, kind: str = "sequel_of") -> None:
    with write_transaction(env.engine) as c:
        relate_works(c, a.work_id, b.work_id, kind, origin="admin", now=NOW)


def unrelate(env: ExportEnv, a: Series, b: Series, kind: str = "sequel_of") -> None:
    with write_transaction(env.engine) as c:
        assert unrelate_works(c, a.work_id, b.work_id, kind)


def push(w: Wired) -> list[int]:
    pushed: list[int] = run(
        RelatedSeries(w.env.engine, w.env.clock, w.events).publish(w.client, w.env.target_id)
    )
    return pushed


def posts(w: Wired) -> list[Any]:
    return [r for r in w.fake.requests_to("/Series/update-related") if r.method == "POST"]


def registry(env: ExportEnv, series_id: str, destination_id: str | None = None) -> Any:
    with env.engine.connect() as c:
        return FieldRegistry(c).get(destination_id or env.destination_id, series_id, RELATED_FIELD)


def two(w: Wired, *, shelf_of_b: str | None = None) -> tuple[Series, Series, int, int]:
    a, b = w.env.new_series("Alpha"), w.env.new_series("Beta", shelf_id=shelf_of_b)
    return a, b, indexed(w, a, "Alpha"), indexed(w, b, "Beta")


@pytest.mark.req("EF-BIB-039")
def test_related_series_pushed_to_kavita() -> None:
    with export_env() as env:
        w = wire(env)
        a, b, ka, kb = two(w)
        relate(env, b, a, "sequel_of")  # Beta follows Alpha
        assert sorted(push(w)) == sorted([ka, kb])
        # the relation is read on both series: Alpha has its sequel, Beta its prequel
        assert w.fake.relations[ka]["sequels"] == [kb]
        assert w.fake.relations[kb]["prequels"] == [ka]
        assert w.fake.violations == []
        assert all(r.headers["x-api-key"] for r in posts(w))  # by the service account's key
        # nothing else was sent: the lists a relation does not name are Kavita's own, read first
        assert len(w.fake.requests_to("/Series/all-related")) == 2
        # a second pass changes nothing and sends nothing
        before = len(w.fake.requests)
        assert push(w) == []
        assert len(w.fake.requests) == before
        # an adaptation and a spin-off are one-sided: Kavita shows the parent by itself
        relate(env, b, a, "adaptation_of")
        push(w)
        assert w.fake.relations[ka]["adaptations"] == [kb]
        assert (
            "adaptations" not in w.fake.relations[kb] or w.fake.relations[kb]["adaptations"] == []
        )
        assert w.fake.violations == []


@pytest.mark.req("EF-BIB-039")
def test_a_relation_removed_is_withdrawn_and_a_third_party_relation_stays() -> None:
    with export_env() as env:
        w = wire(env)
        a, b, ka, kb = two(w)
        third = w.fake.add_series("Gamma", "/manga/Gamma", [])
        w.fake.relations[ka] = {"others": [third], "sequels": [third]}  # a person's own links
        relate(env, b, a, "sequel_of")
        push(w)
        assert w.fake.relations[ka]["sequels"] == sorted([third, kb])
        assert w.fake.relations[ka]["others"] == [third]
        unrelate(env, b, a, "sequel_of")
        assert sorted(push(w)) == sorted([ka, kb])
        assert w.fake.relations[ka]["sequels"] == [third]  # only ours went
        assert w.fake.relations[ka]["others"] == [third]
        assert w.fake.relations[kb].get("prequels", []) == []
        assert w.fake.violations == []


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
def test_relation_never_pushed_between_libraries_open_to_different_profiles() -> None:
    with export_env(max_age_rating=13) as env:  # the family shelf: a child's library
        w = wire(env)
        adult_shelf, adult_destination = add_adult_shelf(env)  # no ceiling: the household's adults
        adult_library = _library_of(env, adult_destination)
        kid, adult = env.new_series("Kid tale"), env.new_series("Grown tale", shelf_id=adult_shelf)
        kk = indexed(w, kid, "Kid tale")
        ka = indexed(
            w, adult, "Grown tale", destination_id=adult_destination, library_id=adult_library
        )
        relate(env, adult, kid, "sequel_of")
        assert push(w) == []
        assert posts(w) == [] and w.fake.requests_to("/Series/all-related") == []
        assert w.fake.relations == {}  # nothing is said, in either direction, about either series
        # the same shelves, now both open to the same profiles: the relation goes
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(max_age_rating=None, allowed_profiles=[]))
        assert sorted(push(w)) == sorted([kk, ka])
        assert w.fake.relations[kk]["sequels"] == [ka]
        # a shelf narrowed to some profiles takes the relation back, at the next pass
        with write_transaction(env.engine) as c:
            c.execute(
                sa.update(export.shelf)
                .where(export.shelf.c.id == adult_shelf)
                .values(allowed_profiles=[new_ulid()])
            )
        assert sorted(push(w)) == sorted([kk, ka])
        assert w.fake.relations[kk].get("sequels", []) == []
        assert w.fake.relations[ka].get("prequels", []) == []
        assert w.fake.violations == []


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
def test_libraries_open_to_the_same_profiles_are_related_across_libraries() -> None:
    with export_env(max_age_rating=None) as env:
        w = wire(env)
        shelf, destination = add_adult_shelf(env)  # `max_age_rating` 99 -> the same ceiling below
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(max_age_rating=None, allowed_profiles=[]))
        library = _library_of(env, destination)
        left, right = env.new_series("Left"), env.new_series("Right", shelf_id=shelf)
        kl = indexed(w, left, "Left")
        kr = indexed(w, right, "Right", destination_id=destination, library_id=library)
        relate(env, left, right, "spin_off_of")
        assert push(w) == [kr]  # Kavita derives the parent: one side is written
        assert w.fake.relations[kr]["spinOffs"] == [kl]


@pytest.mark.req("EF-BIB-039", "ENF-SEC-010")
def test_a_relation_waits_while_paused_and_skips_a_missing_series() -> None:
    with export_env() as env:
        w = wire(env)
        a, b, _ka, kb = two(w)
        relate(env, b, a)
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(paused_reason="forbidden"))
        w.client.set_write_guard(target_write_guard(env.engine, env.target_id))
        assert push(w) == [] and posts(w) == []
        with write_transaction(env.engine) as c:
            c.execute(sa.update(export.shelf).values(paused_reason=None))
        # a series Kavita does not know (the cache outlived it) is left alone, the other is not hurt
        w.fake.series = [s for s in w.fake.series if s["id"] != kb]
        del w.fake.ages[kb]
        assert push(w) == []
        assert w.fake.violations == []
