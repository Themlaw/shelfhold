"""Relecture adverse de L18 : un adulte qui porte un plafond d'âge n'est pas un profil enfant.

La règle de visibilité (`identity.domain.visibility.scope_of`) ne connaît qu'un enfant :
`role == "child"` ; un adulte (`admin`, `member`) voit tout, plafond ou non. La porte de
l'administrateur pour un enfant doit s'arrêter à la même définition : la marque d'un adulte,
même avec un plafond d'âge resté en base, n'est ni listée ni annulable par l'administrateur.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import identity
from tests.content_env import user_id_of
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_l18_undo_read_mark import mark_read, read_unit, state, undo


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    yield from _world(monkeypatch, settings)


def _adult_with_ceiling(world: World, username: str) -> str:
    """A member whose row carries an age ceiling: the schema allows it, the visibility rule
    ignores it (an adult sees everything)."""
    who = user_id_of(world.seed.engine, username)
    with write_transaction(world.seed.engine) as connection:
        connection.execute(
            sa.update(identity.user).where(identity.user.c.id == who).values(max_age_rating=16)
        )
    return who


@pytest.mark.req("EF-LEC-014", "EF-USR-006")
def test_admin_cannot_undo_the_mark_of_an_adult_with_a_ceiling(world: World) -> None:
    unit, carrier = read_unit(world)
    mum = _adult_with_ceiling(world, "mum")
    mark_read(world, "mum", unit, carrier)
    answer = undo(world, world.admin, unit, for_child=mum)
    assert answer.status_code == 200
    assert answer.json() == {"undone": 0}  # an adult's mark is not the administrator's to undo
    assert state(world, "mum", unit) == "read"


@pytest.mark.req("EF-LEC-014", "EF-USR-006")
def test_admin_list_never_shows_the_marks_of_an_adult_with_a_ceiling(world: World) -> None:
    unit, carrier = read_unit(world)
    _adult_with_ceiling(world, "mum")
    mark_read(world, "mum", unit, carrier)
    got = world.admin.get(
        "/api/reader/read-marks", params={"edition_id": world.seed.editions["ok"]}
    )
    assert got.status_code == 200
    assert [i["username"] for i in got.json()["items"]] == []  # another adult's reading
