"""M3 L09 (EF-USR-006, EF-BIB-055): the one filter, below the HTTP surface.

A series, a Unit, an Edition with a suggested age, and an event are asked of the same condition as
a Work. Nothing is filtered after the query: what these functions return is made of visible rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import (
    visible_edition_ids,
    visible_series_ids,
    visible_unit_ids,
    visible_work_ids,
)
from shelfhold.config import Settings
from shelfhold.events.activity import ActivityEvent, Viewer
from shelfhold.events.projection import project_event
from shelfhold.identity.public import scope_of
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.migrate import migrate
from shelfhold.infrastructure.schema import catalog
from tests.visibility_env import Seed, seed_database

NOW = datetime(2026, 11, 2, 9, 0, tzinfo=UTC)
CHILD = scope_of("child", 10)


@pytest.fixture
def seed(settings: Settings) -> Iterator[Seed]:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    migrate(settings)
    seeded = seed_database(settings.database_path)
    yield seeded
    seeded.engine.dispose()


def _series(seed: Seed, *members: str) -> str:
    series_id = new_ulid()
    with seed.engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.series).values(id=series_id, title="Tale shelf", sort_title="tale")
        )
        for position, key in enumerate(members, start=1):
            connection.execute(
                sa.insert(catalog.series_member).values(
                    series_id=series_id, work_id=seed.works[key], position=str(position)
                )
            )
    return series_id


def _unit(seed: Seed, edition_key: str) -> str:
    unit_id = new_ulid()
    with seed.engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.unit).values(
                id=unit_id, edition_id=seed.editions[edition_key], number_raw="1", number_norm="1"
            )
        )
    return unit_id


@pytest.mark.req("EF-USR-006")
def test_series_is_visible_only_when_every_member_is(seed: Seed) -> None:
    all_visible = _series(seed, "ok", "adult_shelf")
    one_hidden = _series(seed, "ok", "above")  # a title that would spoil a hidden member
    empty = _series(seed)
    ids = {all_visible, one_hidden, empty, "01HZZZZZZZZZZZZZZZZZZZZZZM"}
    with seed.engine.connect() as connection:
        assert visible_series_ids(connection, CHILD, ids) == {all_visible}
        assert visible_series_ids(connection, scope_of("member", None), ids) == {
            all_visible, one_hidden,
        }  # fmt: skip
        assert visible_series_ids(connection, scope_of("ghost", None), ids) == set()
        assert visible_series_ids(connection, CHILD, []) == set()


@pytest.mark.req("EF-USR-006")
def test_unit_is_visible_when_its_edition_is(seed: Seed) -> None:
    fine, unrated, adult = (_unit(seed, k) for k in ("ok", "mixed_new", "above"))
    with seed.engine.connect() as connection:
        assert visible_unit_ids(connection, CHILD, {fine, unrated, adult}) == {fine}
        assert visible_unit_ids(connection, scope_of("admin", None), {fine, unrated, adult}) == {
            fine, unrated, adult,
        }  # fmt: skip
        assert visible_unit_ids(connection, scope_of("ghost", None), {fine}) == set()


@pytest.mark.req("EF-USR-006", "EF-BIB-055")
def test_an_open_suggested_age_above_the_ceiling_hides_the_edition(seed: Seed) -> None:
    """A source says an Edition that was approved is more than its rating: it is blocked from the
    family shelves (EF-BIB-055), and to a child it is as if it were not there until it is ruled."""
    edition, work = seed.editions["ok"], seed.works["ok"]
    with seed.engine.connect() as connection:
        assert visible_edition_ids(connection, CHILD, {edition}) == {edition}
    with seed.engine.begin() as connection:
        connection.execute(
            sa.insert(catalog.age_suggestion).values(
                id=new_ulid(), entity_type="edition", entity_id=edition, value=16, x=False,
                provenance="provider", seen_at=NOW, state="open",
            )
        )  # fmt: skip
    with seed.engine.connect() as connection:
        assert visible_edition_ids(connection, CHILD, {edition}) == set()
        assert visible_work_ids(connection, CHILD, {work}) == {work}  # the Work stays rated
    with seed.engine.begin() as connection:  # ruled on (rejected): the Edition is hers again
        connection.execute(sa.update(catalog.age_suggestion).values(state="rejected"))
    with seed.engine.connect() as connection:
        assert visible_edition_ids(connection, CHILD, {edition}) == {edition}


@pytest.mark.req("EF-USR-006")
def test_x_and_the_highest_ceiling(seed: Seed) -> None:
    """X is the strictest age; a ceiling of 99 is X, and 18 is below it: an adult-flagged Work is
    hidden to every child, a 16+ one is hidden under 16 and visible from 16."""
    with seed.engine.connect() as connection:
        every = set(seed.works.values())
        sixteen = visible_work_ids(connection, scope_of("child", 16), every)
        top = visible_work_ids(connection, scope_of("child", 99), every)
        eighteen = visible_work_ids(connection, scope_of("child", 18), every)
    assert seed.works["above"] in sixteen and seed.works["x"] not in sixteen
    assert seed.works["x"] not in eighteen  # X is above 18+
    assert seed.works["x"] in top  # unless the ceiling is X itself
    for never in ("unrated", "orphan", "misfiled", "flagged", "adult_edition"):
        assert seed.works[never] not in top, (
            never
        )  # not rated as a whole, or adult: never a child's


def _event(**payload: object) -> ActivityEvent:
    return ActivityEvent(
        7, "work.updated", "household", payload, owner_id="kid", owner_role="child", title="t"
    )


@pytest.mark.req("EF-USR-006", "EF-USR-016")
def test_projection_asks_the_filter_after_the_role_rules(seed: Seed) -> None:
    kid = Viewer("kid", "child")
    ok, hidden = seed.works["ok"], seed.works["above"]
    with seed.engine.connect() as connection:
        shown = project_event(connection, kid, CHILD, _event(work_id=ok))
        assert shown is not None and shown.title == "t"
        assert project_event(connection, kid, CHILD, _event(work_id=hidden)) is None
        assert project_event(connection, kid, CHILD, _event(work_ids=[ok, hidden])) is None
        # an event that names nothing is not about a Work: the role rules alone decide
        assert project_event(connection, kid, CHILD, _event(state="done")) is not None
        # a payload nested out of reach is not read, so it is not shown
        deep: dict[str, object] = {"work_id": ok}
        for _ in range(8):
            deep = {"inner": deep}
        assert project_event(connection, kid, CHILD, _event(**deep)) is None
        # the Edition a request is about, not named in the payload
        assert project_event(connection, kid, CHILD, _event(), editions=[seed.editions["ok"]])
        assert (
            project_event(connection, kid, CHILD, _event(), editions=[seed.editions["above"]])
            is None
        )
        # a scope that is neither an adult's nor a child's sees nothing, whoever the viewer says
        assert project_event(connection, kid, scope_of("ghost", None), _event(work_id=ok)) is None
        # the role rules still come first: another person's private event is not hers
        other = ActivityEvent(8, "work.updated", "private", {"work_id": ok}, owner_id="other")
        assert project_event(connection, kid, CHILD, other) is None
        # an adult is not narrowed by the filter, only by the role rules
        adult = project_event(
            connection, Viewer("a", "admin"), scope_of("admin", None), _event(work_id=hidden)
        )
        assert adult is not None
