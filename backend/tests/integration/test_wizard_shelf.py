"""M3 ENF-UX-001: the first-run wizard makes the shelf linked to Kavita (`POST /setup/shelf`).

The route is the door of the wizard to what `shelfhold shelf create` does on the host: the same
service (`HeadlessAdmin.shelf_create`: the shelf, its destination under a registered root, the
folder, the Kavita library with the audit of who may read it). The guard is that of every other
door that makes a shelf (`shelf.max_age_rating`, a parental decision: an admin session, never a
key; a fresh one as soon as a child profile exists), and the audit line names it so. A shelf below 18+ stays refused unless Kavita keeps the accounts
apart, and a name or a folder that climbs out of the library is refused before anything is
written.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.headless.errors import ShelfExposedToChildren
from shelfhold.headless.wiring import _ShelfLinker
from shelfhold.infrastructure.schema import export
from shelfhold.ops.wiring import open_ops
from tests.auth_env import AuthEnv, auth_env
from tests.headless_env import KAVITA_PASSWORD, KAVITA_URL
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.l41_env import Instance, instance
from tests.visibility_env import add_profile

ROUTE = "/api/v1/setup/shelf"


@dataclass
class Linked:
    """What the linker was asked: the library the shelf gets, or the accounts that read it."""

    calls: list[str] = field(default_factory=list)
    exposed: tuple[str, ...] = ()


def _linker(settings: Settings, engine: Any, seen: Linked) -> _ShelfLinker:
    class Recording(_ShelfLinker):
        def link(self, target_id: str, destination_id: str, *, library_name: str) -> str:
            seen.calls.append(library_name)
            if seen.exposed:
                raise ShelfExposedToChildren(seen.exposed)
            return "1"

    return Recording(settings, engine)


@dataclass
class World:
    inst: Instance
    api: AuthEnv
    linked: Linked

    def shelves(self) -> list[tuple[str, int | None]]:
        with self.inst.engine.connect() as c:
            rows = c.execute(sa.select(export.shelf.c.name, export.shelf.c.max_age_rating))
            return sorted((r.name, r.max_age_rating) for r in rows)

    def destinations(self) -> int:
        with self.inst.engine.connect() as c:
            return c.execute(
                sa.select(sa.func.count()).select_from(export.shelf_destination)
            ).scalar_one()

    def make(self, **body: Any) -> Any:
        return self.api.post(
            ROUTE, json={"name": "Livres", "max_age": "18+", "subpath": "livres", **body}
        )


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path) -> Iterator[World]:
    seen = Linked()
    with instance(settings, tmp_path) as probe:
        linker = _linker(settings, probe.engine, seen)
    with instance(settings, tmp_path / "wizard", linker=linker) as inst:
        inst.admin.target_add_kavita(
            name="kavita", url=KAVITA_URL, username="u", password=KAVITA_PASSWORD,
            accept_admin_risk=True,
            path_map=[{"local": str(inst.library), "remote": "/manga", "mode": "rw"}],
        )  # fmt: skip
        ops = open_ops(settings, engine=inst.engine, composition=inst.comp)
        with auth_env(monkeypatch, settings, headless_admin=inst.admin, ops_services=ops) as api:
            assert api.login().status_code == 200
            yield World(inst, api, seen)


@pytest.mark.req("ENF-UX-001", "EF-EXP-002")
def test_wizard_creates_the_shelf_linked_to_kavita(world: World) -> None:
    """One call: the shelf with its age ceiling, its destination, its folder, its Kavita library."""
    answer = world.make()
    assert answer.status_code == 201, answer.text
    body = answer.json()
    assert body["created"] is True and body["shelf_id"] and body["destination_id"]
    assert world.shelves() == [("Livres", 18)]
    assert world.destinations() == 1
    assert world.linked.calls == ["Livres"]  # the library is made (or found) and audited
    assert (world.inst.library / "livres").is_dir()  # the folder exists before the first export


@pytest.mark.req("ENF-UX-001")
def test_the_second_call_changes_nothing(world: World) -> None:
    first = world.make().json()
    again = world.make()
    assert again.status_code == 201
    assert again.json()["shelf_id"] == first["shelf_id"] and again.json()["created"] is False
    assert world.destinations() == 1 and world.linked.calls == ["Livres"]


@pytest.mark.req("ENF-UX-001", "EF-EXP-047")
def test_age_ceiling_is_the_own_of_the_shelf_and_kept_by_the_rules(world: World) -> None:
    """The ceiling is read like on the host: a tier of the scale, Famille never above 13+."""
    assert world.make(name="Famille", max_age="18+", subpath="famille").status_code == 422
    assert world.make(max_age="seven").status_code == 422
    assert world.make(max_age="17+").status_code in {400, 422}  # no tier 17 on the scale
    assert world.shelves() == [] and world.destinations() == 0 and world.linked.calls == []
    ok = world.make(name="Famille", max_age="13+", subpath="famille")
    assert ok.status_code == 201, ok.text
    assert world.shelves() == [("Famille", 13)]


@pytest.mark.req("ENF-UX-001", "EF-EXP-055")
def test_audit_of_kavita_access_refuses_a_shelf_children_would_read(world: World) -> None:
    """The audit before the link names the accounts that must not read: nothing is left behind."""
    world.linked.exposed = ("kid8",)
    answer = world.make(name="Famille", max_age="13+", subpath="famille")
    assert answer.status_code in {403, 409, 422}, answer.text
    assert answer.json()["code"] == "target.child_exposure"
    assert world.shelves() == [] and world.destinations() == 0


@pytest.mark.req("ENF-UX-001", "ENF-SEC-004")
@pytest.mark.parametrize("subpath", ["../outside", "/etc", "a/b", "", ".hidden", "x" * 200])
def test_a_folder_that_leaves_the_library_is_refused_before_anything_is_written(
    world: World, subpath: str
) -> None:
    answer = world.make(subpath=subpath)
    assert answer.status_code == 422, answer.text
    assert world.shelves() == [] and world.destinations() == 0 and world.linked.calls == []


@pytest.mark.req("ENF-UX-001", "EF-USR-024")
def test_refused_to_a_member_and_to_a_child(world: World) -> None:
    for role, ip in (("member", "10.0.0.5"), ("child", "10.0.0.6")):
        client = add_profile(
            world.api, role, role, ip, **({"max_age_rating": 10} if role == "child" else {})
        )
        answer = client.post(
            ROUTE,
            json={"name": "Livres", "max_age": "18+", "subpath": "livres"},
            headers={"X-CSRF-Token": world.api.csrf(client)},
        )
        assert answer.status_code == 403, (role, answer.text)
        assert answer.json()["code"] == "forbidden"
    assert world.shelves() == [] and world.linked.calls == []


@pytest.mark.req("ENF-UX-001", "EF-USR-024")
def test_refused_to_an_api_key_even_an_admin_one(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    key = _create_key(monkeypatch, capsys, world.api.settings)
    answer = world.api.other_client("10.0.0.9").post(
        ROUTE, json={"name": "Livres", "max_age": "18+", "subpath": "livres"}, headers=_bearer(key)
    )
    assert answer.status_code in {401, 403}, answer.text
    assert answer.json()["code"] in {"credential_not_allowed", "forbidden"}
    assert world.shelves() == [] and world.linked.calls == []


@pytest.mark.req("ENF-UX-001", "EF-USR-024")
def test_needs_a_session_and_a_fresh_one(world: World) -> None:
    body = {"name": "Livres", "max_age": "18+", "subpath": "livres"}
    assert world.api.other_client("10.0.0.8").post(ROUTE, json=body).status_code == 401
    add_profile(world.api, "child", "child", "10.0.0.6", max_age_rating=10)  # a child exists
    world.api.clock.advance(timedelta(minutes=20))  # the password was typed 20 minutes ago
    stale = world.api.client.post(
        ROUTE, json=body, headers={"X-CSRF-Token": world.api.csrf(world.api.client)}
    )
    assert stale.status_code == 401 and stale.json()["code"] == "reauth_required"
    assert world.shelves() == [] and world.linked.calls == []


@pytest.mark.req("ENF-UX-001", "EF-USR-024")
def test_the_creation_leaves_a_line_in_the_audit(world: World) -> None:
    assert world.make().status_code == 201
    with world.inst.engine.connect() as c:
        actions = c.execute(sa.text("SELECT action FROM audit")).scalars().all()
    assert "shelf.max_age_rating" in actions  # the guard's line: a parental decision
    assert "admin_risk.accept" not in actions
    assert "shelf.create" in actions  # the service's own, in the transaction of the change
