"""#383 (M5 L21) : preuves du contradicteur sur les sélections enregistrées.

Chaque test `xfail(strict=True)` montre un trou réel de la branche ; il doit passer (et perdre son
marqueur) quand le trou est fermé.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

from datetime import UTC, datetime

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, reading
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    api,
    world,
)
from tests.integration.test_m5_l21_saved_selections import (
    HASH,
    SELECTIONS,
    _make,
    _titles,
    _user_id,
)

pytestmark = pytest.mark.req("EF-BIB-017")


def _units(c: sa.Connection, edition_id: str, count: int, start: int = 1) -> list[str]:
    found = []
    for number in range(count):
        unit_id = new_ulid()
        found.append(unit_id)
        c.execute(
            sa.insert(catalog.unit).values(
                id=unit_id, edition_id=edition_id, number_raw=str(number + start),
                number_norm=str(number + start),
            )
        )  # fmt: skip
    return found


def _read(c: sa.Connection, user_id: str, units: list[str]) -> None:
    c.execute(
        sa.insert(reading.reading_position).values(
            id=new_ulid(), user_id=user_id, carrier_type="content", carrier_id=new_ulid(),
            content_hash=HASH, locator={}, client_ts=datetime.now(UTC), read_units=units,
        )
    )  # fmt: skip


def test_reading_state_of_a_child_is_decided_by_the_editions_she_sees(world: World) -> None:
    """`ok` est visible de l'enfant par ses Éditions `ok` et `ok_second` ; `ok_reserved` (étagère
    réservée à un autre profil) lui est cachée. Elle a lu toutes les Unités de `ok_reserved` (lu
    avant la réservation, ou par une liseuse) et rien des Éditions qu'elle voit. La règle écrite
    pour l'étagère et la langue (« ne comptent que par une Édition qu'elle voit ») doit valoir pour
    l'état : `ok` est « non lu » pour elle, jamais « lu » par l'Édition cachée."""
    kid = world.kid
    with world.seed.engine.begin() as c:
        hidden = _units(c, world.seed.editions["ok_reserved"], 2)
        _units(c, world.seed.editions["ok"], 2)
        _read(c, _user_id(world, "kid"), hidden)

    def kid_sees(states: list[str]) -> list[str]:
        made = _make(world, kid, "mine", {"reading_state": states})
        answer = kid.get(f"{SELECTIONS}/{made}")
        assert answer.status_code == 200, answer.text
        world.clean(answer.text)
        return _titles(answer)

    ok = world.seed.title_of("ok")
    assert ok not in kid_sees(["read"])
    assert ok in kid_sees(["unread"])


def test_the_full_export_decides_about_saved_selections() -> None:
    """La table est une donnée du membre, comme `collection` (exportée) : l'export complet doit
    la porter, ou dire pourquoi il la laisse. Aujourd'hui elle n'est dans aucune des deux listes,
    et `test_every_table_is_either_exported_or_left_out_with_a_reason` est rouge sur la branche."""
    from shelfhold.platform.export_all import EXCLUDED_TABLES, EXPORTED_TABLES

    assert "saved_selection" in set(EXPORTED_TABLES) | set(EXCLUDED_TABLES)


def test_a_unit_removed_upstream_never_keeps_a_work_from_being_read(world: World) -> None:
    """L'adulte a lu les deux Unités lisibles de l'Édition `adult_shelf` ; la troisième a été
    retirée à la source (`availability = removed`, `state = source_removed`) : personne ne pourra
    jamais la lire. L'Œuvre doit être « lue », pas « en cours » pour toujours."""
    admin = world.env.client
    with world.seed.engine.begin() as c:
        readable = _units(c, world.seed.editions["adult_shelf"], 2)
        gone = _units(c, world.seed.editions["adult_shelf"], 1, start=3)
        c.execute(
            sa.update(catalog.unit).where(catalog.unit.c.id.in_(gone))
            .values(availability="removed", state="source_removed")
        )  # fmt: skip
        admin_id = str(
            c.execute(sa.text("SELECT id FROM user WHERE role = 'admin' LIMIT 1")).scalar()
        )
        _read(c, admin_id, readable)
    made = _make(world, admin, "lus", {"reading_state": ["read"]})
    answer = admin.get(f"{SELECTIONS}/{made}")
    assert answer.status_code == 200, answer.text
    assert world.seed.title_of("adult_shelf") in _titles(answer)
