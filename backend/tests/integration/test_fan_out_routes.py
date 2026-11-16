"""EF-EXP-002, EF-USR-024, ENF-SEC-010 (M4 L06a): the routes that bind a shelf to destinations.

Linking a destination decides who can read what (a shelf below 18+ goes to a server that keeps
accounts apart, or nowhere), so `shelf.bind_destination` is a reserved action: an admin session,
fresh, never a key and never a child or a member. The attacks: a child, a member (even one the
shelf is open to), a key (read-only or not), a profile that reads another's, a tree of another
shelf, and a shelf that does not exist (the same answer for everyone who may not know).
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.identity.public import RESERVED_ACTIONS
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, platform
from tests.auth_env import AuthEnv, auth_env
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.visibility_env import add_profile

UNKNOWN = "01HZZZZZZZZZZZZZZZZZZZZZZZ"


@dataclass
class World:
    api: AuthEnv
    engine: sa.Engine
    library: Path
    family: str  # a shelf below 18+
    adult: str
    server: str  # keeps accounts apart
    loose: str  # does not
    other: str  # another one that does

    def path(self, shelf_id: str, suffix: str = "") -> str:
        return f"/api/v1/shelves/{shelf_id}/destinations{suffix}"

    def bind(self, shelf_id: str, subpath: str, *, target: str | None = None, **body: Any) -> Any:
        (self.library / subpath).mkdir(exist_ok=True)
        return self.api.post(
            self.path(shelf_id),
            json={"target_id": target or self.server, "root_subpath": subpath, **body},
        )

    def count(self) -> int:
        with self.engine.connect() as connection:
            return connection.execute(
                sa.select(sa.func.count()).select_from(export.shelf_destination)
            ).scalar_one()


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path) -> Iterator[World]:
    with auth_env(monkeypatch, settings) as api:
        assert api.login().status_code == 200
        engine = create_db_engine(settings.database_path)
        library = tmp_path / "library" / "manga"
        library.mkdir(parents=True)
        family, adult, server, loose, other = (new_ulid() for _ in range(5))
        path_map = [{"local": str(library), "remote": "/manga", "mode": "rw"}]
        with write_transaction(engine) as connection:
            connection.execute(
                sa.insert(platform.physical_root).values(
                    root_id=new_ulid(), path_entry=str(library.parent), mode="rw"
                )
            )
            connection.execute(
                sa.insert(export.shelf),
                [
                    {"id": family, "name": "Famille", "phrase": "Pour tous", "max_age_rating": 13},
                    {"id": adult, "name": "Adultes", "phrase": "Adultes", "max_age_rating": 99},
                ],
            )
            connection.execute(
                sa.insert(export.export_target),
                [
                    {"id": server, "kind": "kavita", "name": "Server", "path_map": path_map},
                    {"id": loose, "kind": "plugin_x", "name": "Loose", "path_map": path_map},
                    {"id": other, "kind": "kavita", "name": "Other", "path_map": path_map},
                ],
            )
        yield World(api, engine, library, family, adult, server, loose, other)
        engine.dispose()


@pytest.mark.req("EF-EXP-002", "EF-USR-024", "ENF-SEC-010")
def test_the_binding_action_is_in_the_truth_table() -> None:
    action = RESERVED_ACTIONS["shelf.bind_destination"]
    assert action.name == "shelf.bind_destination"


@pytest.mark.req("EF-EXP-002", "EF-EXP-047")
def test_an_admin_binds_a_shelf_to_several_destinations_and_lists_them(world: World) -> None:
    first = world.bind(world.adult, "one")
    second = world.bind(world.adult, "two", target=world.loose)
    assert first.status_code == 201 and second.status_code == 201, (first.text, second.text)
    listed = world.api.client.get(world.path(world.adult))
    assert listed.status_code == 200, listed.text
    rows = {d["id"]: d for d in listed.json()["destinations"]}
    assert set(rows) == {first.json()["id"], second.json()["id"]}
    assert {d["role"] for d in rows.values()} == {"primary"}
    assert len({d["tree_id"] for d in rows.values()}) == 2  # two folders, two trees
    # the family shelf: a server that partitions nothing is refused, with its code
    refused = world.bind(world.family, "kids", target=world.loose)
    assert refused.status_code in {403, 409, 422}
    assert refused.json()["code"] == "shelf.destination_not_partitioned"
    assert world.bind(world.family, "kids").status_code == 201


@pytest.mark.req("EF-EXP-002", "EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("role", ["child", "member"])
def test_binding_routes_refused_to_child_member_and_non_admin_key(
    world: World, role: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A child and a member, even one the shelf is open to; a key (read-only or not)."""
    extra = {"max_age_rating": 10} if role == "child" else {}
    client = add_profile(world.api, role, role, "10.0.0.5", **extra)
    headers = {"X-CSRF-Token": world.api.csrf(client)}
    body = {"target_id": world.server, "root_subpath": "x"}
    (world.library / "x").mkdir()
    for shelf in (world.family, world.adult, UNKNOWN):
        post = client.post(world.path(shelf), json=body, headers=headers)
        get = client.get(world.path(shelf))
        assert post.status_code == 403 and get.status_code == 403, (shelf, post.text, get.text)
        assert post.json()["code"] == "forbidden" == get.json()["code"]  # no oracle on the shelf
    assert world.count() == 0

    for flags in ((), ("--read-only",)):
        key = _create_key(monkeypatch, capsys, world.api.settings, *flags)
        other = world.api.other_client("10.0.0.9")
        post = other.post(world.path(world.adult), json=body, headers=_bearer(key))
        get = other.get(world.path(world.adult), headers=_bearer(key))
        assert post.status_code in {401, 403} and get.status_code in {401, 403}, flags
        assert post.json()["code"] in {"credential_not_allowed", "forbidden"}
    assert world.count() == 0


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_needs_a_session_and_a_fresh_one(world: World) -> None:
    (world.library / "x").mkdir()
    body = {"target_id": world.server, "root_subpath": "x"}
    anonymous = world.api.other_client("10.0.0.8")
    assert anonymous.post(world.path(world.adult), json=body).status_code == 401
    assert anonymous.get(world.path(world.adult)).status_code == 401
    add_profile(world.api, "kid", "child", "10.0.0.6", max_age_rating=10)  # a child exists
    world.api.clock.advance(timedelta(minutes=20))
    stale = world.api.client.post(
        world.path(world.adult), json=body, headers={"X-CSRF-Token": world.api.csrf()}
    )
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert world.count() == 0


@pytest.mark.req("EF-EXP-002", "EF-USR-024")
def test_a_tree_of_another_shelf_is_never_attached_to(world: World) -> None:
    """IDOR between shelves: the id of a tree that is not the shelf's own refuses, says nothing of
    the other shelf's bindings, and the list of a shelf never shows another's."""
    mine = world.bind(world.family, "mine")
    theirs = world.bind(world.adult, "theirs")
    assert mine.status_code == theirs.status_code == 201
    trees = {
        d["id"]: d["tree_id"]
        for shelf in (world.family, world.adult)
        for d in world.api.client.get(world.path(shelf)).json()["destinations"]
    }
    stolen = world.api.post(
        world.path(world.family),
        json={
            "target_id": world.other,
            "root_subpath": "theirs",
            "tree_id": trees[theirs.json()["id"]],
        },
    )
    assert stolen.status_code == 409 and stolen.json()["code"] == "target.tree_overlap"
    unknown = world.api.post(
        world.path(world.family),
        json={"target_id": world.server, "root_subpath": "mine", "tree_id": UNKNOWN},
    )
    assert unknown.status_code in {404, 409, 422}
    seen = world.api.client.get(world.path(world.family)).json()["destinations"]
    assert [d["id"] for d in seen] == [mine.json()["id"]]
    assert world.count() == 2
    assert world.api.client.get(world.path(UNKNOWN)).status_code == 404  # the admin may know


@pytest.mark.req("EF-EXP-002", "EF-USR-024")
def test_the_binding_leaves_a_line_in_the_audit(world: World) -> None:
    assert world.bind(world.adult, "one").status_code == 201
    with world.engine.connect() as connection:
        actions = connection.execute(sa.text("SELECT action FROM audit")).scalars().all()
    assert "shelf.bind_destination" in actions  # the guard's line
    assert "shelf.bind" in actions  # the service's, in the transaction of the change
