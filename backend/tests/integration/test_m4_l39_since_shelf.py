"""M4 L39, D-P3: what a shelf takes away from a child, `?since=` tells (EF-USR-006, EF-INT-001).

A change of the profiles a shelf is open to, or of the shelf of an Edition, writes one event per
child whose view of a Work moved. The removals of a child read her own events only, never tell her
to drop what was given back, and never reach a child who kept the Work.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.catalog.public import removed_ids, track_shelf_changes, window
from shelfhold.identity.public import reader_scope
from shelfhold.infrastructure.schema import catalog
from tests.integration import test_m4_l39_allowed_profiles as l39
from tests.integration.test_m4_l39_allowed_profiles import World

pytestmark = pytest.mark.req("EF-USR-006", "EF-INT-001")

WORKS = "/api/v1/works"
world = l39.world


def _token(client: object) -> str:
    return str(client.get(WORKS, params={"limit": 200}).json()["since"])  # type: ignore[attr-defined]


def _set(world: World, profiles: list[str]) -> None:
    answer = world.env.post(
        f"/api/v1/shelves/{world.seed.shelves['adult']}/allowed-profiles",
        json={"profiles": profiles},
    )
    assert answer.status_code == 200, answer.text


def _removed(world: World, client: object, token: str) -> list[str]:
    answer = client.get(WORKS, params={"since": token, "limit": 200})  # type: ignore[attr-defined]
    assert answer.status_code == 200, answer.text
    return list(answer.json()["removed"])


def test_only_the_child_who_lost_the_shelf_is_told(world: World) -> None:
    work = world.seed.works["adult_shelf"]
    kid_token, sib_token = _token(world.kid), _token(world.sib)
    _set(world, [world.sib_id])
    assert work in _removed(world, world.kid, kid_token)
    assert work not in _removed(world, world.sib, sib_token)  # she kept it: nothing to drop


def test_a_shelf_opened_again_is_not_a_removal(world: World) -> None:
    work = world.seed.works["adult_shelf"]
    token = _token(world.kid)
    _set(world, [world.sib_id])
    _set(world, [])  # given back before she syncs
    assert work not in _removed(world, world.kid, token)


def test_a_work_given_after_the_token_is_not_a_removal(world: World) -> None:
    work = world.seed.works["adult_shelf"]
    _set(world, [world.sib_id])  # hidden from the kid before her token
    token = _token(world.kid)
    _set(world, [])
    _set(world, [world.sib_id])
    assert work not in _removed(world, world.kid, token)  # she never had it at the token


def test_an_edition_moved_to_a_reserved_shelf_is_a_removal(world: World) -> None:
    work, edition = world.seed.works["adult_shelf"], world.seed.editions["adult_shelf"]
    with world.seed.engine.connect() as connection:
        token = window(connection).head
        scope = reader_scope(connection, world.kid_id)
    with (
        world.seed.engine.begin() as connection,
        track_shelf_changes(connection, edition_ids=[edition]),
    ):
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == edition)
            .values(shelf_id=world.shelf_id)  # the shelf of `sib` alone
        )
    with world.seed.engine.connect() as connection:
        assert work in removed_ids(connection, scope, token)
        assert work not in removed_ids(connection, reader_scope(connection, world.sib_id), token)


def test_a_write_that_changes_nothing_writes_nothing(world: World) -> None:
    with world.seed.engine.begin() as connection:
        before = window(connection).head
        with track_shelf_changes(connection, shelf_ids=[world.seed.shelves["adult"]]):
            pass
        assert window(connection).head == before
