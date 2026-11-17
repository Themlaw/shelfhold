"""M4 L18b (EF-EXP-025, ENF-SEC-010): who says that a profile's private list goes public.

A profile's list is private by default, in Shelfhold and in Kavita. Only the admin (a session, or a
key of scope `admin`) may approve its promotion there: a child, a member, a read-only key, a key
of the profile itself and an anonymous caller are refused, whoever the list belongs to, and the
refusal does not say whether the list exists. Adding an item to an approved list takes the approval
back. Real application, real sessions, real keys.
"""

# ruff: noqa: F811  (the fixtures `api` and `world` are those of the child-surface suite)
from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from tests.auth_env import CSRF_HEADER
from tests.integration.test_child_surfaces import (  # noqa: F401  (fixtures)
    World,
    _shape,
    api,
    world,
)
from tests.integration.test_m4_l18a_collections import (
    COLLECTIONS,
    _key_client,
    _make,
    _post,
)
from tests.visibility_env import add_profile


def _approve(
    world: World, client: Any, collection: str, approved: bool = True, **extra: Any
) -> Any:
    return client.put(
        f"{COLLECTIONS}/{collection}/kavita-public",
        json={"approved": approved},
        headers={CSRF_HEADER: world.env.csrf(client), **extra},
    )


def _approved(world: World, collection: str) -> bool:
    with world.seed.engine.connect() as c:
        return (
            c.execute(
                sa.select(catalog.collection.c.public_approved_at).where(
                    catalog.collection.c.id == collection
                )
            ).scalar_one()
            is not None
        )


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_only_an_admin_approves_a_private_list_for_kavita(world: World) -> None:
    works = world.seed.works
    ann = add_profile(world.env, "ann", "member", "10.0.0.11")
    mine = _make(world, ann, "Ann's list", "profile", [works["ok"]])
    # a child, a member (the owner herself), and a stranger member: refused, the list is unchanged
    bob = add_profile(world.env, "bob", "member", "10.0.0.12")
    for client in (world.kid, ann, bob):
        assert _approve(world, client, mine).status_code == 403
    assert not _approved(world, mine)
    # a key of the owner, read-only or not, and of the admin without the scope `admin`: refused
    ann_client, ann_auth = _key_client(world, "ann", "10.0.0.61", read_only=True)
    assert ann_client.put(
        f"{COLLECTIONS}/{mine}/kavita-public", json={"approved": True}, headers=ann_auth
    ).status_code in (401, 403)
    adm_client, adm_auth = _key_client(world, "admin", "10.0.0.62", read_only=True)  # a read key
    assert adm_client.put(
        f"{COLLECTIONS}/{mine}/kavita-public", json={"approved": True}, headers=adm_auth
    ).status_code in (401, 403)
    # an anonymous caller gets nothing
    anonymous = world.env.other_client("10.0.0.63")
    assert (
        anonymous.put(f"{COLLECTIONS}/{mine}/kavita-public", json={"approved": True}).status_code
        == 401
    )
    assert not _approved(world, mine)
    # the admin's session does it, and takes it back
    assert _approve(world, world.env.client, mine).status_code == 200
    assert _approved(world, mine)
    assert _approve(world, world.env.client, mine, False).json() == {"approved": False}
    assert not _approved(world, mine)
    # a key of scope `admin` of the admin is the admin's word as well (as `plans`)
    full_client, full_auth = _key_client(world, "admin", "10.0.0.65")
    assert (
        full_client.put(
            f"{COLLECTIONS}/{mine}/kavita-public", json={"approved": True}, headers=full_auth
        ).status_code
        == 200
    )
    assert _approved(world, mine)


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_the_refusal_does_not_say_which_list_exists(world: World) -> None:
    ann = add_profile(world.env, "ann", "member", "10.0.0.11")
    mine = _make(world, ann, "Ann's list", "profile", [world.seed.works["ok"]])
    absent = new_ulid()
    for client in (world.kid, add_profile(world.env, "bob", "member", "10.0.0.12")):
        a = _approve(world, client, mine)
        b = _approve(world, client, absent)
        assert _shape(a) == _shape(b) and a.status_code == 403
    # for the admin an absent identifier is 404, and a household collection has nothing to approve
    assert _approve(world, world.env.client, absent).status_code == 404
    shared = _make(world, world.env.client, "Household", "shared", [world.seed.works["ok"]])
    assert _approve(world, world.env.client, shared).status_code == 409


@pytest.mark.req("EF-EXP-025", "ENF-SEC-010")
def test_an_item_added_to_an_approved_list_takes_the_approval_back(world: World) -> None:
    works = world.seed.works
    ann = add_profile(world.env, "ann", "member", "10.0.0.11")
    mine = _make(world, ann, "Ann's list", "profile", [works["ok"]])
    assert _approve(world, world.env.client, mine).status_code == 200
    # a removal leaves it; an addition (by her or by anyone) is a new list the admin has not seen
    added = _post(world, ann, f"{COLLECTIONS}/{mine}/items", {"work_id": works["mixed"]})
    assert added.status_code == 201
    assert not _approved(world, mine)
