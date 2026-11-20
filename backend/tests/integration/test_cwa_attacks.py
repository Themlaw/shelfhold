"""EF-EXP-022, EF-EXP-047, EF-USR-024, ENF-SEC-010 (M4 L09): what is tried against a CWA drop box.

Who may bind one (a reserved action: an admin session, never a child, a member or a key, read-only
or not); what a hand-made plan may cite (nothing of another shelf: IDOR); and where a drop may go
(the folder the target declares, with the sub-folder of the binding, never outside it: not by
`..`, not by a link, not by a name with a slash).
"""

# ruff: noqa: F811  (the `world` fixture of L06a is imported, then asked for by name)
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from shelfhold_sdk.fsops import PlannedContent

from shelfhold.export.application.fan_out import EpubFile
from shelfhold.export.application.plan_executor import PlanBinding, PlanRefusedError
from shelfhold.export.targets.cwa import CwaDestination
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.cwa_env import (
    INGEST,
    add_book,
    bind_cwa,
    folder,
    names,
    other_shelf_item,
    records,
    rig,
)
from tests.export_env import export_env
from tests.integration.test_auth_keys_reserved import _bearer, _create_key
from tests.integration.test_fan_out_routes import World, world  # noqa: F401
from tests.visibility_env import add_profile


def write(rel: str, item: str) -> dict[str, object]:
    return {"op": "write", "rel_path": rel, "content_ref": {"kind": "export_item", "ref": item}}


# -- who binds ---------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {
        "SHELFHOLD_DISK_PAUSE_PERCENT": "0",
        "SHELFHOLD_DISK_PAUSE_GIB": "0",
        "SHELFHOLD_DATA_DIR": str(tmp_path / "data"),
        "SHELFHOLD_STORE_DIR": str(tmp_path / "store"),
        "SHELFHOLD_LIBRARY_DIR": str(tmp_path / "library"),
    }


def cwa_in_world(world: World) -> str:
    target = new_ulid()
    path_map = [{"local": str(world.library), "remote": "/ingest", "mode": "rw"}]
    with write_transaction(world.engine) as connection:
        connection.execute(
            sa.insert(export.export_target).values(
                id=target, kind="cwa", name="Drop box", path_map=path_map
            )
        )
    return target


@pytest.mark.req("EF-EXP-022", "EF-EXP-047", "EF-USR-024", "ENF-SEC-010")
def test_an_admin_binds_an_adult_shelf_and_a_family_shelf_is_a_409(world: World) -> None:
    target = cwa_in_world(world)
    bound = world.bind(world.adult, INGEST, target=target, transport="dropbox")
    assert bound.status_code == 201, bound.text
    refused = world.bind(world.family, "kids", target=target, transport="dropbox")
    assert refused.status_code == 409
    assert refused.json()["code"] == "shelf.destination_not_partitioned"
    wrong = world.bind(world.adult, "wrong", target=target, transport="tree")
    assert wrong.status_code == 409 and wrong.json()["code"] == "target.transport_not_supported"


@pytest.mark.req("EF-EXP-022", "EF-EXP-047", "EF-USR-024", "ENF-SEC-010")
@pytest.mark.parametrize("role", ["child", "member"])
def test_a_child_a_member_or_a_key_binds_no_drop_box(
    world: World, role: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target = cwa_in_world(world)
    extra = {"max_age_rating": 10} if role == "child" else {}
    client = add_profile(world.api, role, role, "10.0.0.5", **extra)
    headers = {"X-CSRF-Token": world.api.csrf(client)}
    (world.library / INGEST).mkdir()
    body = {"target_id": target, "root_subpath": INGEST, "transport": "dropbox"}
    for shelf in (world.family, world.adult, "01HZZZZZZZZZZZZZZZZZZZZZZZ"):
        post = client.post(world.path(shelf), json=body, headers=headers)
        assert post.status_code == 403 and post.json()["code"] == "forbidden", (shelf, post.text)
    assert world.count() == 0
    for flags in ((), ("--read-only",)):
        key = _create_key(monkeypatch, capsys, world.api.settings, *flags)
        other = world.api.other_client("10.0.0.9")
        post = other.post(world.path(world.adult), json=body, headers=_bearer(key))
        assert post.status_code in {401, 403}, flags
    assert world.count() == 0


@pytest.mark.req("EF-EXP-022", "ENF-SEC-010")
@pytest.mark.parametrize("subpath", ["../outside", "a/../../outside", "/etc"])
def test_a_sub_folder_that_climbs_is_refused_when_bound(world: World, subpath: str) -> None:
    target = cwa_in_world(world)
    (world.library.parent / "outside").mkdir(exist_ok=True)
    answer = world.api.post(
        world.path(world.adult),
        json={"target_id": target, "root_subpath": subpath, "transport": "dropbox"},
    )
    assert answer.status_code in {409, 422}, answer.text
    assert world.count() == 0


# -- what a plan may cite -----------------------------------------------------------------------


def hand_made(env, destination: str, item: str, *refs: str) -> PlanBinding:  # type: ignore[no-untyped-def]
    caps = CwaDestination.capabilities
    with env.engine.connect() as connection:
        target = connection.execute(
            sa.select(export.shelf_destination.c.target_id).where(
                export.shelf_destination.c.id == destination
            )
        ).scalar_one()
    binding = PlanBinding(destination, target, caps, {r: "epub" for r in refs})
    env.pipeline.plan_ledger.plan(
        "hand",
        destination,
        PlannedContent(
            planned_refs=frozenset({item}), egress="foyer", watermarked_refs=frozenset()
        ),
    )
    return binding


@pytest.mark.req("EF-EXP-022", "EF-EXP-047", "ENF-SEC-010")
def test_idor_an_item_of_another_shelf_is_never_dropped() -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        mine = add_book(env, env.new_series("Synthetic Book"), "1", store)
        foreign = other_shelf_item(env, store)
        destination = bind_cwa(env)
        fan, _source = rig(env, store)
        fan.run(env.shelf_id, "c1")
        assert names(env) == [f"Synthetic Book [{mine.item_id}].epub"]  # only this shelf's item
        for line in records(env, destination):
            assert line.export_item_id == mine.item_id
        # a plan that cites it anyway (a destination that lies) is refused, and nothing is dropped
        binding = hand_made(env, destination, mine.item_id, mine.item_id, foreign.item_id)
        with pytest.raises(PlanRefusedError):
            env.pipeline.execute_destination_plan(
                binding, "hand", {"ops": [write(f"x [{foreign.item_id}].epub", foreign.item_id)]}
            )
        assert names(env) == [f"Synthetic Book [{mine.item_id}].epub"]
        env.pipeline.plan_ledger.release("hand")


# -- where a drop may go ---------------------------------------------------------------------------


@pytest.mark.req("EF-EXP-022", "ENF-SEC-010")
@pytest.mark.parametrize(
    "rel",
    ["../escape.epub", "sub/escape.epub", "/abs/escape.epub", "a\\b.epub", ".hidden.epub", "x.cbz"],
)
def test_a_drop_goes_to_the_folder_itself_under_a_plain_name(rel: str) -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        book = add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_cwa(env)
        rig(env, store)
        binding = hand_made(env, destination, book.item_id, book.item_id)
        with pytest.raises(PlanRefusedError):
            env.pipeline.execute_destination_plan(
                binding, "hand", {"ops": [write(rel, book.item_id)]}
            )
        assert names(env) == []
        assert not (env.lib.manga / "escape.epub").exists()
        assert not (env.lib.manga / INGEST / "sub").exists()
        env.pipeline.plan_ledger.release("hand")


def fan_pass(env, store, destination: str):  # type: ignore[no-untyped-def]
    fan, _source = rig(env, store)
    return next(o for o in fan.run(env.shelf_id, "c1").outcomes if o.destination_id == destination)


@pytest.mark.req("EF-EXP-022", "ENF-SEC-010")
def test_a_folder_changed_to_a_link_after_the_binding_is_never_written_through(
    tmp_path: Path,
) -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_cwa(env)
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        folder(env).rmdir()
        os.symlink(outside, folder(env))  # the ingestion folder became a link to somewhere else
        outcome = fan_pass(env, store, destination)
        assert outcome.status == "failed"
        assert list(outside.iterdir()) == [] and records(env, destination) == []


@pytest.mark.req("EF-EXP-022", "ENF-SEC-010")
def test_a_link_inside_the_path_of_the_binding_is_never_written_through(tmp_path: Path) -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        (env.lib.manga / "inner").mkdir()
        destination = bind_cwa(env, subpath="inner/ingest")
        other = env.lib.manga / "other"
        other.mkdir()
        (env.lib.manga / "inner" / "ingest").rmdir()
        (env.lib.manga / "inner" / "ingest").symlink_to(other)  # another folder of the library
        outcome = fan_pass(env, store, destination)
        assert outcome.status == "failed" and list(other.iterdir()) == []
        assert records(env, destination) == []


@pytest.mark.req("EF-EXP-022", "ENF-SEC-010")
def test_a_sub_folder_tampered_in_the_database_to_climb_writes_nothing() -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_cwa(env)
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.update(export.shelf_destination)
                .where(export.shelf_destination.c.id == destination)
                .values(root_subpath="../..")
            )
        outcome = fan_pass(env, store, destination)
        assert outcome.status == "failed"
        assert [p for p in env.lib.library.rglob("*.epub") if "received" not in p.parts] == []


@pytest.mark.req("EF-EXP-022", "ENF-SEC-010")
def test_a_link_planted_at_the_name_of_a_drop_is_never_followed(tmp_path: Path) -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        book = add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_cwa(env)
        target = tmp_path / "victim.txt"
        target.write_text("keep me")
        link = folder(env) / f"Synthetic Book [{book.item_id}].epub"
        link.symlink_to(target)  # the name the drop would take is a link to a file elsewhere
        outcome = fan_pass(env, store, destination)
        assert outcome.status == "failed"
        assert target.read_text() == "keep me" and records(env, destination) == []


@pytest.mark.req("EF-EXP-022")
def test_an_ingestion_folder_that_does_not_exist_is_not_made() -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        add_book(env, env.new_series("Synthetic Book"), "1", store)
        destination = bind_cwa(env)
        folder(env).rmdir()
        outcome = fan_pass(env, store, destination)
        assert outcome.status == "failed" and not folder(env).exists()
