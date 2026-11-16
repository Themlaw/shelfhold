"""EF-EXP-010, EF-EXP-002, EF-USR-024, ENF-SEC-010 (M4 L07): who may create a native folder and bind
a shelf to it.

A folder written by Shelfhold is a reserved action: an admin session, fresh, never a key, never a
child or a member. The attacks: a child, a member (even one the shelf is open to), a key (read-only
or not), the family shelf that a folder keeping no accounts apart may not serve, a folder that
covers the local source of Suwayomi, and a target that does not exist.
"""

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import RootGuard
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, export
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.integration.test_fan_out_routes import UNKNOWN, World, world  # noqa: F401
from tests.visibility_env import add_profile

TARGETS = "/api/v1/targets"


def native_target(world: World) -> str:
    target = new_ulid()
    path_map = [{"local": str(world.library), "remote": "/local", "mode": "rw"}]
    with write_transaction(world.engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(
                id=target, kind="native_folder", name="Native", path_map=path_map
            )
        )
    return target


@pytest.mark.req("EF-EXP-010", "EF-EXP-002")
def test_an_admin_binds_an_adult_shelf_to_a_native_folder(world: World) -> None:
    target = native_target(world)
    bound = world.bind(world.adult, "local", target=target)
    assert bound.status_code == 201, bound.text
    listed = world.api.client.get(world.path(world.adult)).json()["destinations"]
    assert [d["id"] for d in listed] == [bound.json()["id"]]
    refused = world.bind(world.family, "kids", target=target)  # a folder keeps no accounts apart
    assert refused.status_code == 409 and refused.json()["code"] == "shelf.destination_not_partitioned"


@pytest.mark.req("EF-EXP-010", "EF-EXP-002", "EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("role", ["child", "member"])
def test_a_child_or_a_member_creates_no_native_destination(
    world: World, role: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = native_target(world)
    extra = {"max_age_rating": 10} if role == "child" else {}
    client = add_profile(world.api, role, role, "10.0.0.5", **extra)
    headers = {"X-CSRF-Token": world.api.csrf(client)}
    (world.library / "x").mkdir()
    bind = {"target_id": target, "root_subpath": "x"}
    new_target = {"kind": "native_folder", "name": "Mine", "path_map": [{"local": str(world.library / "x"), "remote": "/x", "mode": "rw"}]}
    for shelf in (world.family, world.adult, UNKNOWN):
        post = client.post(world.path(shelf), json=bind, headers=headers)
        assert post.status_code == 403 and post.json()["code"] == "forbidden", (shelf, post.text)
    created = client.post(TARGETS, json=new_target, headers=headers)
    assert created.status_code == 403, created.text
    assert world.count() == 0 and _targets(world) == 1  # the one of the fixture

    for flags in ((), ("--read-only",)):
        key = _create_key(monkeypatch, capsys, world.api.settings, *flags)
        other = world.api.other_client("10.0.0.9")
        post = other.post(world.path(world.adult), json=bind, headers=_bearer(key))
        made = other.post(TARGETS, json=new_target, headers=_bearer(key))
        assert post.status_code in {401, 403} and made.status_code in {401, 403}, flags
    assert world.count() == 0 and _targets(world) == 1


def _targets(world: World) -> int:
    with world.engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT count(*) FROM export_target WHERE kind = 'native_folder'")
        ).scalar_one()


@pytest.mark.req("EF-EXP-010", "EF-EXP-002")
def test_an_admin_creates_a_native_folder_target_and_the_guard_applies(world: World) -> None:
    root = world.library / "native"
    root.mkdir()
    body = {"kind": "native_folder", "name": "Mihon", "path_map": [{"local": str(root), "remote": "/native", "mode": "rw"}]}
    with write_transaction(world.engine) as connection:  # a Suwayomi whose local source is read
        connection.execute(sa.insert(acquisition.source_config).values(plugin_id="suwayomi"))
        RootGuard().record(connection, "suwayomi", str(root / "local"))
    refused = world.api.post(TARGETS, json=body)
    assert refused.status_code == 409 and refused.json()["code"] == "target.root_overlaps_local_source"
    assert _targets(world) == 0

    with write_transaction(world.engine) as connection:
        RootGuard().record(connection, "suwayomi", str(world.library.parent / "elsewhere"))
    created = world.api.post(TARGETS, json=body)
    assert created.status_code == 201, created.text
    assert _targets(world) == 1


@pytest.mark.req("EF-EXP-010", "EF-EXP-002")
def test_binding_a_native_folder_that_covers_the_local_source_is_a_409(world: World) -> None:
    target = native_target(world)
    (world.library / "local" / "deep").mkdir(parents=True)
    with write_transaction(world.engine) as connection:
        connection.execute(sa.insert(acquisition.source_config).values(plugin_id="suwayomi"))
        RootGuard().record(connection, "suwayomi", str(world.library / "local" / "deep"))
    refused = world.bind(world.adult, "local", target=target)
    assert refused.status_code == 409 and refused.json()["code"] == "target.root_overlaps_local_source"
    assert world.count() == 0
    assert world.bind(world.adult, "other", target=target).status_code == 201
