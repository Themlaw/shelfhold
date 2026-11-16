"""#303 (M4 L06a, fan-out): the holes found by the adversarial review, one red PoC each.

Each test said what should hold and failed (`xfail(strict=True)`); the fix of the lot turned it
green and the marker is gone.
"""

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import present_in
from shelfhold.export.application.remove_destination import (
    RemovalMode,
    forget_destination,
    removal_phrase,
)
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from tests.detach_env import ADMIN, detach_env
from tests.export_env import ExportEnv, export_env
from tests.fan_out_env import add_target, bind, records_of, service
from tests.integration.test_fan_out_plans import item_of, setup, write_plan
from tests.rename_env import export_chapters


def _tree(env: ExportEnv, destination_id: str) -> str:
    tree_id = {b.destination_id: b.tree_id for b in service(env).list_bindings(env.shelf_id)}[
        destination_id
    ]
    assert tree_id is not None
    return tree_id


@pytest.mark.req("EF-EXP-002", "EF-EXP-006")
def test_two_secondaries_outlive_their_primary_and_the_shelf_is_still_listed() -> None:
    """A tree with a primary and two secondaries; the primary is removed (keep the files). The
    cascade detaches both, the next `ensure_trees` makes the first one a primary of a new tree on
    the same folder and then raises `TreeOverlapError` for the second, in the same transaction:
    nothing is kept, so every later `list_bindings` (the GET route, a 500) and every fan-out pass
    (`_fan_out_pass` catches only a pause) fails again, and the shelves after this one are never
    fanned out."""
    with export_env() as env:
        tree_id = _tree(env, env.destination_id)
        first = bind(env, "Kavita", target_id=add_target(env), attach_to_tree=tree_id)
        second = bind(env, "Kavita", target_id=add_target(env), attach_to_tree=tree_id)
        with write_transaction(env.engine) as connection:
            forget_destination(connection, env.destination_id)

        views = {v.destination_id: v for v in service(env).list_bindings(env.shelf_id)}

        assert set(views) == {first, second}
        assert len({v.tree_id for v in views.values()}) == 1  # still one tree, one folder
        assert sorted(str(v.role) for v in views.values()) == ["primary", "secondary"]


@pytest.mark.req("EF-EXP-031", "EF-BIB-044")
def test_deleting_the_files_of_a_primary_leaves_no_line_that_says_they_are_there() -> None:
    """`remove --delete-files` on the primary of a tree a second server indexes: the files go
    (the preview says nothing of the other server), the secondary keeps its copied lines in
    `written`, `present_in` says the Units are still there, and the next pass, which makes it the
    primary of a new tree, writes nothing back: its server shows an empty folder while the manifest
    says it is full (until a reconciliation of that folder, if one runs)."""
    with detach_env() as d:
        env = d.env
        export_chapters(env, "Dr Stone", ["1", "2"])
        tree_id = _tree(env, env.destination_id)
        secondary = bind(env, "Kavita", target_id=add_target(env), attach_to_tree=tree_id)
        assert len(records_of(env, secondary)) == 2

        d.remover.apply(
            env.destination_id, RemovalMode.DELETE_FILES, principal=ADMIN,
            typed=removal_phrase(env.destination_id, RemovalMode.DELETE_FILES),
        )  # fmt: skip
        assert env.files() == []  # the files are gone for good, for both servers

        with env.engine.connect() as connection:
            units = list(connection.execute(sa.select(catalog.unit.c.id)).scalars())
            held = [u for u in units if present_in(connection, u, secondary)]
        written = [r.rel_path for r in records_of(env, secondary) if r.state == "written"]
        service(env).list_bindings(env.shelf_id)  # the next pass gives it a tree of its own
        rewritten = len(env.pipeline.run(secondary, job_id=new_ulid()).ops)

        assert (held, written) == ([], []) or rewritten == 2  # honest manifest, or files back


@pytest.mark.req("EF-EXP-002", "EF-BIB-037")
def test_a_plan_that_writes_an_item_elsewhere_moves_its_manifest_line() -> None:
    """A destination writes an item at `Old/…`, then (a new template, a new folder) at `New/…`.
    `record_applied` updates only a line in `planned`/`error`/`missing`: the manifest still says
    `Old/…`, the file at `New/…` is known to nobody. A withdrawal (a shelf below 18+ whose Edition
    is rated above the ceiling) and `remove --delete-files` read the manifest: they take `Old/…`
    and leave `New/…` in the folder the children read. Not the declared gap (`rename`/`recycle`):
    this is a plain `write`."""
    with export_env() as env:
        fan, plans, contents, _probe = setup(env)
        item = item_of(env, env.new_series("A"), "1", b"one", contents)
        plans.by_destination[env.destination_id] = write_plan(item, folder="Old")
        fan.run(env.shelf_id, new_ulid())
        plans.by_destination[env.destination_id] = write_plan(item, folder="New")
        fan.run(env.shelf_id, new_ulid())

        on_disk = sorted(str(p.relative_to(env.lib.manga / "Kavita")) for p in env.files())
        manifest = sorted(str(r.rel_path) for r in records_of(env, env.destination_id))

        assert "New/Volume 1.cbz" in on_disk
        assert set(on_disk) <= set(manifest)  # every file the plans put there is in the manifest
