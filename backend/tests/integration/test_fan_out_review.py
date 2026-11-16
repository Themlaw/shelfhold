"""EF-EXP-002, EF-EXP-031, EF-BIB-044 (M4 L06a, relecture adverse de #303): what the fixes keep.

The PoCs of `test_review_pr303.py` say the three big holes are closed; here the rest: the tree
handed to a secondary, a read that never writes, the manifest that follows a rename and a bin, the
barrier read again while a plan runs, the bytes served per binding, one writer in a withdrawal.
"""

from dataclasses import dataclass

import pytest
import sqlalchemy as sa

from shelfhold.export.application.fan_out import (
    HostContentStore,
    TreeBarrier,
    ensure_trees,
    planned_content,
)
from shelfhold.export.application.plan_executor import PlanRefusedError
from shelfhold.export.application.remove_destination import (
    RemovalMode,
    forget_destination,
)
from shelfhold.export.application.withdrawal import _files
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.sdk.fsops import ContentRef, PlannedContent
from tests.detach_env import detach_env
from tests.export_env import ExportEnv, export_env
from tests.family_env import family_env, rate
from tests.fan_out_env import add_target, bind, records_of, service
from tests.integration.test_fan_out_plans import item_of, setup, write_plan
from tests.rename_env import export_chapters


def _tree(env: ExportEnv) -> str:
    tree_id = {b.destination_id: b.tree_id for b in service(env).list_bindings(env.shelf_id)}[
        env.destination_id
    ]
    assert tree_id is not None
    return tree_id


def _count(env: ExportEnv, table: sa.Table) -> int:
    with env.engine.connect() as connection:
        return connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


@pytest.mark.req("EF-EXP-002", "EF-EXP-006")
def test_the_tree_is_handed_to_a_secondary_when_its_primary_goes() -> None:
    with export_env() as env:
        tree_id = _tree(env)
        first = bind(env, "Kavita", target_id=add_target(env), attach_to_tree=tree_id)
        second = bind(env, "Kavita", target_id=add_target(env), attach_to_tree=tree_id)
        with write_transaction(env.engine) as connection:
            forget_destination(connection, env.destination_id)

        views = {v.destination_id: v for v in service(env).read_bindings(env.shelf_id)}  # no write

        assert {v.tree_id for v in views.values()} == {tree_id}
        assert {k: v.role for k, v in views.items()} == {first: "primary", second: "secondary"}
        with write_transaction(env.engine) as connection:  # the last one takes the tree with it
            forget_destination(connection, first)
            forget_destination(connection, second)
        assert _count(env, export.tree) == 0 == _count(env, export.tree_indexer)


@pytest.mark.req("EF-EXP-002")
def test_a_read_never_writes_and_a_pass_that_meets_orphans_does_not_fail() -> None:
    with export_env() as env:
        tree_id = _tree(env)
        bind(env, "Kavita", target_id=add_target(env), attach_to_tree=tree_id)
        with write_transaction(env.engine) as connection:  # two bindings, one folder, no tree
            connection.execute(sa.delete(export.tree))  # the cascade detaches both

        views = service(env).read_bindings(env.shelf_id)
        assert {v.tree_id for v in views} == {None} and _count(env, export.tree) == 0

        with write_transaction(env.engine) as connection:  # the pass: no error, a tree is made
            made = ensure_trees(connection, env.shelf_id)
        assert len({v.tree_id for v in made if v.tree_id}) == 1


def _plan(*ops: dict[str, object]) -> dict[str, object]:
    return {"ops": list(ops)}


@pytest.mark.req("EF-EXP-002", "EF-EXP-041")
def test_the_manifest_follows_a_rename_and_a_bin_and_the_indexers_copy_it() -> None:
    with export_env() as env:
        fan, plans, contents, _probe = setup(env)
        item = item_of(env, env.new_series("A"), "1", b"one " * 40, contents)
        secondary = bind(env, "Kavita", target_id=add_target(env), attach_to_tree=_tree(env))
        plans.by_destination[env.destination_id] = write_plan(item, folder="Old")
        fan.run(env.shelf_id, new_ulid())

        plans.by_destination[env.destination_id] = _plan(
            {"op": "mkdir", "rel_path": "New"},
            {"op": "rename", "rel_path": "Old/Volume 1.cbz", "dst_rel_path": "New/Volume 1.cbz"},
        )
        assert fan.run(env.shelf_id, new_ulid()).outcomes[0].status == "done"
        for destination in (env.destination_id, secondary):
            (line,) = records_of(env, destination)
            assert (line.rel_path, line.state) == ("New/Volume 1.cbz", "written")

        plans.by_destination[env.destination_id] = _plan(
            {"op": "recycle", "rel_path": "New/Volume 1.cbz"}
        )
        fan.run(env.shelf_id, new_ulid())
        assert env.files() == []
        for destination in (env.destination_id, secondary):
            (line,) = records_of(env, destination)
            assert line.state == "missing"  # present_in says nobody holds it


@dataclass
class _CeilingLowered:
    """The barrier of a shelf, which holds for the first operation only."""

    calls: int = 0

    def holds(self, _destination_id: str, _planned: PlannedContent) -> bool:
        self.calls += 1
        return self.calls <= 1


@pytest.mark.req("EF-EXP-047", "EF-EXP-002")
def test_the_barrier_is_read_again_while_a_plan_runs() -> None:
    with export_env() as env:
        fan, plans, contents, probe = setup(env)
        env.pipeline.use_plan_executor(contents=contents, probe=probe, barrier=_CeilingLowered())
        series = env.new_series("A")
        one = item_of(env, series, "1", b"one " * 40, contents)
        two = item_of(env, series, "2", b"two " * 40, contents)
        plans.by_destination[env.destination_id] = write_plan(one, two)

        outcome = fan.run(env.shelf_id, new_ulid()).outcomes[0]

        assert (outcome.status, outcome.code) == ("failed", "export.plan_refused")
        assert [p.name for p in env.files()] == ["Volume 1.cbz"]  # the rest never left
        assert [r.export_item_id for r in records_of(env, env.destination_id)] == []


@pytest.mark.req("EF-EXP-047", "EF-EXP-002")
def test_the_tree_barrier_lets_go_when_the_ceiling_drops_or_the_attestation_goes() -> None:
    with family_env(13) as env:
        _fan, _plans, contents, _probe = setup(env)
        series = env.new_series("Mine")
        rate(env, series, 10)
        kept = item_of(env, series, "1", b"mine " * 40, contents)
        barrier = TreeBarrier(env.engine)
        with env.engine.connect() as connection:
            planned = planned_content(connection, env.destination_id)
        assert kept in planned.planned_refs and barrier.holds(env.destination_id, planned)
        rate(env, series, 18)  # rated above the ceiling since the plan was made
        assert not barrier.holds(env.destination_id, planned)


@pytest.mark.req("EF-EXP-002", "EF-EXP-047")
def test_the_host_serves_no_item_of_another_shelf_to_a_binding() -> None:
    with export_env() as env:
        _fan, _plans, contents, _probe = setup(env)
        other = "01HZZZZZZZZZZZZZZZZZZZZZZY"
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO shelf (id, name, phrase, max_age_rating) "
                    "VALUES (:i, 'Elsewhere', 'Elsewhere', 99)"
                ),
                {"i": other},
            )
        theirs = item_of(env, env.new_series("Theirs", shelf_id=other), "1", b"x" * 50, contents)
        with pytest.raises(PlanRefusedError, match="no such content to this binding"):
            HostContentStore(env.engine).read(
                env.destination_id, ContentRef(kind="export_item", ref=theirs)
            )


@pytest.mark.req("EF-EXP-002", "EF-EXP-006")
def test_a_withdrawal_reads_the_lines_of_the_primary_only() -> None:
    with export_env() as env:
        fan, plans, contents, _probe = setup(env)
        series = env.new_series("A")
        item = item_of(env, series, "1", b"one " * 40, contents)
        secondary = bind(env, "Kavita", target_id=add_target(env), attach_to_tree=_tree(env))
        plans.by_destination[env.destination_id] = write_plan(item)
        fan.run(env.shelf_id, new_ulid())
        assert len(records_of(env, secondary)) == 1  # the copy is there

        with env.engine.connect() as connection:
            files = _files(connection, series.edition_id, env.shelf_id)

        assert [f.destination_id for f in files] == [env.destination_id]  # one file, one intent


@pytest.mark.req("EF-EXP-031", "EF-BIB-044")
def test_the_preview_says_other_servers_read_the_folder_and_an_indexer_deletes_nothing() -> None:
    with detach_env() as d:
        env = d.env
        export_chapters(env, "Dr Stone", ["1", "2"])
        tree_id = _tree(env)
        secondary = bind(env, "Kavita", target_id=add_target(env), attach_to_tree=tree_id)

        preview = d.remover.preview(env.destination_id, RemovalMode.DELETE_FILES)
        assert ("indexed_elsewhere", 1) in {(w.code, w.count) for w in preview.warnings}
        again = d.remover.preview(secondary, RemovalMode.DELETE_FILES)
        assert {w.code for w in again.warnings} >= {"files_kept"}  # an indexer keeps the files
