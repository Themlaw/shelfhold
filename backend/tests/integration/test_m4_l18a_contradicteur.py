"""M4 L18a, relecture adverse (#315): collections and reading lists, read as a profile.

Each test is a hole found by the adverse review, written as the behaviour the lot promises
(docs/decisions/M4.md, L18a) and marked `xfail(strict=True)`: it is red today, and turns the suite
red the day it is fixed without the mark being lifted.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog
from tests.auth_env import ADMIN
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    MISSING,
    World,
    api,
    world,
)
from tests.integration.test_m4_l18a_collections import (
    COLLECTIONS,
    _delete,
    _force_items,
    _key_client,
    _make,
    _post,
)


@pytest.mark.req("EF-BIB-016", "EF-USR-006", "ENF-SEC-010")
@pytest.mark.xfail(
    strict=True,
    reason="#315 contradicteur 1: is_admin comes from the role alone, a read key of the admin "
    "reads every profile's private list (elsewhere an admin power by key needs the `admin` scope)",
)
def test_a_read_key_of_the_admin_does_not_open_a_childs_private_list(world: World) -> None:
    works = world.seed.works
    mine = _make(world, world.kid, "Kid's secret list", "profile", [works["ok"]])
    client, auth = _key_client(world, ADMIN, "10.0.0.71", read_only=True)
    # a key handed to a reading app reads as a program, not as the admin's session
    assert client.get(f"{COLLECTIONS}/{mine}", headers=auth).status_code == 404
    assert mine not in client.get(COLLECTIONS, headers=auth).text


@pytest.mark.req("EF-BIB-016", "EF-USR-006", "ENF-SEC-010")
@pytest.mark.xfail(
    strict=True,
    reason="#315 contradicteur 2: a household list that holds only hidden Works is a 404 on GET "
    "but a 403 on DELETE and POST items (`_writable` does not apply `_present`): an oracle",
)
def test_a_household_list_absent_for_her_is_absent_on_every_door(world: World) -> None:
    works = world.seed.works
    closed = _make(world, world.env.client, "Closed", "shared")
    _force_items(world, closed, [works["above"], works["x"]])
    path = f"{COLLECTIONS}/{closed}"
    assert world.kid.get(path).status_code == 404
    absent_delete = _delete(world, world.kid, f"{COLLECTIONS}/{MISSING}")
    assert absent_delete.status_code == 404
    assert _delete(world, world.kid, path).status_code == absent_delete.status_code
    assert (
        _post(world, world.kid, f"{path}/items", {"work_id": works["ok"]}).status_code
        == _post(
            world, world.kid, f"{COLLECTIONS}/{MISSING}/items", {"work_id": works["ok"]}
        ).status_code
    )


@pytest.mark.req("EF-BIB-016", "EF-USR-006", "ENF-SEC-010")
@pytest.mark.xfail(
    strict=True,
    reason="#315 contradicteur 3: `add_item` answers `position = count(all items) + 1`, hidden "
    "ones included: the gap the read hides comes back in the write's answer",
)
def test_adding_to_her_list_does_not_count_the_hidden_items(world: World) -> None:
    works = world.seed.works
    mine = _make(world, world.kid, "Mine", "profile", [works["ok"]])
    # the admin raises the Work's age after she added it: it leaves what she is shown
    with world.seed.engine.begin() as c:
        c.execute(
            sa.update(catalog.work).where(catalog.work.c.id == works["ok"]).values(age_rating=16)
        )
    added = _post(world, world.kid, f"{COLLECTIONS}/{mine}/items", {"work_id": works["mixed"]})
    assert added.status_code == 201, added.text
    read = world.kid.get(f"{COLLECTIONS}/{mine}").json()
    assert read["count"] == 1 and read["items"][0]["position"] == 1
    assert added.json()["position"] == 1  # today: 2, one hidden item before it
