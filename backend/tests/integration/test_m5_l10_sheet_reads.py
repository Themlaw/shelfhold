"""M5 L10 (EF-UX-006): the series sheet names the Unit its "Lire" button opens.

The sheet is the admin's (a child, a member and a key keep their 403, with no new field of it
reachable); it names the first Unit of the Edition that has a file, by number, and nothing when
there is none. Which file is read, and in full or as an excerpt, is the server's choice on the
publication route (L06), never the sheet's.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import storage
from tests.content_env import add_carrier
from tests.integration.test_l43_series_sheet_api import World, world  # noqa: F401

pytestmark = pytest.mark.req("EF-UX-006")


def _unit_of(world: World, content_id: str) -> str:
    with world.seed.engine.connect() as connection:
        return str(
            connection.execute(
                sa.select(storage.content.c.unit_id).where(storage.content.c.id == content_id)
            ).scalar_one()
        )


def test_the_sheet_names_no_unit_when_the_edition_has_no_file(world: World) -> None:  # noqa: F811
    assert world.sheet()["first_unit_id"] is None


def test_the_sheet_names_the_first_unit_that_has_a_file(world: World) -> None:  # noqa: F811
    first = add_carrier(world.seed.engine, world.edition)
    add_carrier(world.seed.engine, world.edition)
    assert world.sheet()["first_unit_id"] == _unit_of(world, first)


def test_the_sheet_stays_the_admins(world: World) -> None:  # noqa: F811
    add_carrier(world.seed.engine, world.edition)
    assert world.kid.get(world.sheet_path).status_code == 403
    assert world.member.get(world.sheet_path).status_code == 403
