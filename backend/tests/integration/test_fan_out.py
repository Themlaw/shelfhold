"""EF-EXP-002, EF-EXP-047, EF-BIB-044 (M4 L06a, intouchable): one shelf, several bindings.

A binding is a folder and a server. The manifest, the `file_hash` and the rendering are per tree
(ADR-0052): two bindings on two folders are two trees, each written by its own primary; a second
server on the same folder is a secondary indexer that copies the state of the primary in the same
transaction (T9) and never writes (T3). A tree is one copy (T11), `present_in` is per binding.

The barrier of a shelf below 18+ is looked at again for every binding, at every planning: never
inherited from the shelf, and an indexer that keeps no accounts apart stops the whole tree (T10).
"""

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import present_in
from shelfhold.export.domain.bindings import (
    TreeOverlapError,
    UnpartitionedDestinationError,
)
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.export_env import ExportEnv, export_env
from tests.family_env import family_env, rate, rate_edition
from tests.fan_out_env import (
    add_target,
    bind,
    records_of,
    service,
    set_partitioning,
    tree_of,
)

FILE_FIELDS = ("rel_path", "file_hash", "content_hash", "size", "state", "format")


def run(env: ExportEnv, destination_id: str) -> int:
    return len(env.pipeline.run(destination_id, job_id=new_ulid()).ops)


def blocked(env: ExportEnv, destination_id: str) -> list[str]:
    return sorted({b.code for b in env.pipeline.preview(destination_id).blocked})


def folder(env: ExportEnv, subpath: str) -> list[str]:
    return sorted(p.name for p in env.lib.files_under(env.lib.manga / subpath))


def line(row: sa.Row[tuple[object, ...]]) -> tuple[object, ...]:
    return tuple(getattr(row, name) for name in FILE_FIELDS)


def destination_replicas(env: ExportEnv, unit_id: str) -> int:
    with env.engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT count(*) FROM replica WHERE unit_id = :u AND location = 'destination'"),
            {"u": unit_id},
        ).scalar_one()


# ----------------------------------------------------------------------- the fan-out


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_fan_out_one_shelf_many_destinations() -> None:
    """Two folders: two trees, each with its own manifest line and its own file. A third server on
    the first folder is no third copy: it copies the state of the primary and writes nothing."""
    with export_env() as env:
        series = env.new_series()
        first_unit = env.add_chapter(series, "1")
        svc = service(env)
        second = bind(env, "Kavita2")
        before = {b.destination_id: b for b in svc.list_bindings(env.shelf_id)}
        assert set(before) == {env.destination_id, second}
        assert {b.role for b in before.values()} == {"primary"}
        assert before[env.destination_id].tree_id != before[second].tree_id

        assert run(env, env.destination_id) == 1 and run(env, second) == 1
        assert len(folder(env, "Kavita")) == 1 and len(folder(env, "Kavita2")) == 1
        (one,) = records_of(env, env.destination_id)
        (two,) = records_of(env, second)
        assert one.state == two.state == "written" and one.rel_path and two.rel_path
        assert one.shelf_destination_id != two.shelf_destination_id  # a line per binding

        # a server that indexes the folder of the first one: secondary, with what is already there
        third = bind(env, "Kavita", attach_to_tree=before[env.destination_id].tree_id, link=True)
        roles = {b.destination_id: b.role for b in svc.list_bindings(env.shelf_id)}
        assert roles == {env.destination_id: "primary", second: "primary", third: "secondary"}
        assert tree_of(env, third) == before[env.destination_id].tree_id
        assert [line(r) for r in records_of(env, third)] == [line(one)]  # copied at the attach

        # a second chapter: the primary writes it, the secondary gets the same line (one commit)
        env.add_chapter(series, "2")
        assert run(env, third) == 0  # an indexer never writes into the tree
        assert run(env, env.destination_id) == 1
        assert len(folder(env, "Kavita")) == 2
        primary_lines = [line(r) for r in records_of(env, env.destination_id)]
        assert len(primary_lines) == 2
        assert [line(r) for r in records_of(env, third)] == primary_lines  # T9: one file state
        assert folder(env, "Kavita") == sorted(p.name for p in env.files())  # nothing written twice

        # T11: a tree is one copy, whatever the number of its indexers
        assert destination_replicas(env, first_unit) == 2  # two trees, not three bindings


@pytest.mark.req("EF-EXP-002", "EF-EXP-058")
def test_a_second_server_on_a_folder_needs_a_choice_and_the_same_shelf() -> None:
    """Without `attach_to_tree` the same folder is `target.tree_overlap`; a tree of another shelf
    is never attached to (T2), and a server indexes a tree once (T9)."""
    with export_env() as env:
        svc = service(env)
        tree_id = {b.destination_id: b.tree_id for b in svc.list_bindings(env.shelf_id)}[
            env.destination_id
        ]
        assert tree_id is not None
        with pytest.raises(TreeOverlapError):
            bind(env, "Kavita")  # the same folder, nobody said "attach"
        other_shelf = new_ulid()
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.insert(export.shelf).values(
                    id=other_shelf, name="Autre", phrase="Autre", max_age_rating=99
                )
            )
        with pytest.raises(TreeOverlapError):  # T2: two shelves on one tree stay refused
            bind(env, "Kavita", attach_to_tree=tree_id, shelf_id=other_shelf)
        server = add_target(env)
        bind(env, "Kavita", target_id=server, attach_to_tree=tree_id)
        with pytest.raises(TreeOverlapError):  # T9: the same server twice
            bind(env, "Kavita", target_id=server, attach_to_tree=tree_id)
        with pytest.raises(TreeOverlapError):  # a folder that is not the one of the tree
            (env.lib.manga / "Elsewhere").mkdir()
            bind(env, "Elsewhere", attach_to_tree=tree_id)
        with env.engine.connect() as connection:
            count = connection.execute(
                sa.select(sa.func.count()).select_from(export.shelf_destination)
            ).scalar_one()
        assert count == 2  # the first binding and the one secondary: the refusals left no row


@pytest.mark.req("EF-EXP-002")
def test_two_bindings_of_a_shelf_never_share_a_partial_folder() -> None:
    """The several bindings of a shelf are allowed; the overlap rules are not lifted for it."""
    with export_env() as env:
        (env.lib.manga / "Kavita" / "Inside").mkdir()
        with pytest.raises(TreeOverlapError):
            bind(env, "Kavita/Inside")


# ------------------------------------------------------------------------ present_in


@pytest.mark.req("EF-BIB-044")
def test_present_in_per_binding() -> None:
    """`present_in(unit, binding)`: a record of that binding that says the file is there. One
    binding going missing marks nothing on a binding of another tree; the secondary of a tree
    follows its primary (one file, one state)."""
    with export_env() as env:
        series = env.new_series()
        unit = env.add_chapter(series, "1")
        second = bind(env, "Kavita2")
        tree_id = {b.destination_id: b.tree_id for b in service(env).list_bindings(env.shelf_id)}[
            env.destination_id
        ]
        secondary = bind(env, "Kavita", attach_to_tree=tree_id)

        def present() -> dict[str, bool]:
            with env.engine.connect() as connection:
                return {
                    name: present_in(connection, unit, destination)
                    for name, destination in (
                        ("first", env.destination_id),
                        ("second", second),
                        ("secondary", secondary),
                    )
                }

        assert present() == {"first": False, "second": False, "secondary": False}
        run(env, env.destination_id)
        assert present() == {"first": True, "second": False, "secondary": True}
        run(env, second)
        assert present() == {"first": True, "second": True, "secondary": True}

        with write_transaction(env.engine) as connection:  # the second tree loses its file
            connection.execute(
                sa.update(export.export_record)
                .where(export.export_record.c.shelf_destination_id == second)
                .values(state="missing")
            )
        assert present() == {"first": True, "second": False, "secondary": True}

        with write_transaction(env.engine) as connection:  # an indexer's own trouble stays its own
            connection.execute(
                sa.update(export.export_record)
                .where(export.export_record.c.shelf_destination_id == secondary)
                .values(error_code="target.unreachable")
            )
        assert present() == {"first": True, "second": False, "secondary": True}
        primary_line = records_of(env, env.destination_id)[0]
        assert primary_line.error_code is None and primary_line.state == "written"


@pytest.mark.req("EF-BIB-044")
def test_present_in_reads_an_edition_per_binding() -> None:
    """What the screen and the purge ask: the Units of an Edition that a binding holds."""
    from shelfhold.acquisition.public import present_units_in

    with export_env() as env:
        series = env.new_series()
        one, two = env.add_chapter(series, "1"), env.add_chapter(series, "2")
        second = bind(env, "Kavita2")
        run(env, env.destination_id)
        with env.engine.connect() as connection:
            assert present_units_in(connection, series.edition_id, env.destination_id) == {
                one,
                two,
            }
            assert present_units_in(connection, series.edition_id, second) == frozenset()


# --------------------------------------------------------- the barrier, per binding


@pytest.mark.req("EF-EXP-047", "EF-EXP-002")
def test_family_shelf_fan_out_rechecks_partitioning_per_binding() -> None:
    """A shelf below 18+ takes no binding that keeps accounts apart badly, and the check is made
    again at every planning for each of them: an attestation withdrawn, or an indexer of the same
    tree that partitions nothing, stops what is written there and nowhere else."""
    with family_env(13) as env:
        series = env.new_series()
        env.add_chapter(series, "1")
        rate(env, series, 10)

        # at the binding: a destination that partitions nothing is refused, whatever the shelf had
        loose = add_target(env, kind="plugin_x", partitioning="none")
        with pytest.raises(UnpartitionedDestinationError):
            bind(env, "Kavita2", target_id=loose)
        with env.engine.connect() as connection:
            assert (
                connection.execute(
                    sa.select(sa.func.count()).select_from(export.shelf_destination)
                ).scalar_one()
                == 1
            )

        # at the planning: a second binding that was fine, whose attestation is withdrawn
        kept = add_target(env, kind="plugin_x", partitioning="library")
        second = bind(env, "Kavita2", target_id=kept)
        assert blocked(env, second) == []
        set_partitioning(env, kept, "library")  # withdraws the attestation: `none` again
        assert blocked(env, second) == ["shelf.destination_not_partitioned"]
        assert run(env, second) == 0 and folder(env, "Kavita2") == []
        assert run(env, env.destination_id) == 1  # the other binding does not pay for it

        # T10: a secondary that partitions nothing stops the writing into the tree it indexes
        env.add_chapter(series, "2")
        tree_id = {b.destination_id: b.tree_id for b in service(env).list_bindings(env.shelf_id)}[
            env.destination_id
        ]
        indexer_target = add_target(env, kind="plugin_x", partitioning="library")
        indexer = bind(env, "Kavita", target_id=indexer_target, attach_to_tree=tree_id)
        assert blocked(env, env.destination_id) == []
        set_partitioning(env, indexer_target, "library")  # withdrawn: the indexer shows all
        assert blocked(env, env.destination_id) == ["shelf.destination_not_partitioned"]
        assert run(env, env.destination_id) == 0 and len(folder(env, "Kavita")) == 1
        assert indexer


@pytest.mark.req("EF-EXP-047", "EF-EXP-002")
def test_secondary_that_partitions_nothing_is_refused_on_a_family_tree() -> None:
    with family_env(13) as env:
        tree_id = {b.destination_id: b.tree_id for b in service(env).list_bindings(env.shelf_id)}[
            env.destination_id
        ]
        loose = add_target(env, kind="plugin_x", partitioning="none")
        with pytest.raises(UnpartitionedDestinationError):
            bind(env, "Kavita", target_id=loose, attach_to_tree=tree_id)
        assert [b.role for b in service(env).list_bindings(env.shelf_id)] == ["primary"]


@pytest.mark.req("EF-EXP-047", "EF-EXP-002")
def test_family_shelf_to_a_non_family_binding_is_refused_never_inherited() -> None:
    """A family shelf is never attached to the binding of an adult shelf's tree, and what an
    adult shelf holds never reaches a family binding: an Edition above the ceiling, unrated, or
    filed elsewhere is planned for none of the family bindings (point m5 of #299)."""
    with family_env(13) as env:
        second = bind(env, "Kavita2")
        mine = env.new_series("Mine")
        env.add_chapter(mine, "1")
        rate(env, mine, 10)
        above = env.new_series("Above")
        env.add_chapter(above, "1")
        rate(env, above, 18)
        unrated = env.new_series("Unrated")
        env.add_chapter(unrated, "1")
        adult_shelf = new_ulid()
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.insert(export.shelf).values(
                    id=adult_shelf, name="Elsewhere", phrase="Elsewhere", max_age_rating=99
                )
            )
        elsewhere = env.new_series("Elsewhere", shelf_id=adult_shelf)
        env.add_chapter(elsewhere, "1")
        rate(env, elsewhere, 18)

        for destination in (env.destination_id, second):
            assert run(env, destination) == 1  # "Mine", and nothing else
            assert set(blocked(env, destination)) == {
                "shelf.above_ceiling",
                "shelf.rating_required",
            }
        with env.engine.connect() as connection:
            editions = set(
                connection.execute(
                    sa.select(export.export_item.c.edition_id).select_from(
                        export.export_item.join(
                            export.export_record,
                            export.export_record.c.export_item_id == export.export_item.c.id,
                        )
                    )
                ).scalars()
            )
        assert editions == {mine.edition_id}
        # raising an Edition above the ceiling after the fact changes nothing already written, and
        # nothing new goes out on either binding
        rate_edition(env, mine.edition_id, 16)
        env.add_chapter(mine, "2")
        assert run(env, env.destination_id) == 0 and run(env, second) == 0


@pytest.mark.req("EF-EXP-002", "EF-EXP-006")
@pytest.mark.parametrize("forgotten", ["primary", "secondary"])
def test_forgetting_a_binding_of_a_tree_leaves_the_others_standing(forgotten: str) -> None:
    from shelfhold.export.application.remove_destination import forget_destination

    with export_env() as env:
        tree_id = {b.destination_id: b.tree_id for b in service(env).list_bindings(env.shelf_id)}[
            env.destination_id
        ]
        secondary = bind(env, "Kavita", target_id=add_target(env), attach_to_tree=tree_id)
        gone = env.destination_id if forgotten == "primary" else secondary
        with write_transaction(env.engine) as connection:
            forget_destination(connection, gone)
        with env.engine.connect() as connection:
            ids = set(connection.execute(sa.select(export.shelf_destination.c.id)).scalars())
            indexers = set(
                connection.execute(sa.select(export.tree_indexer.c.shelf_destination_id)).scalars()
            )
        assert gone not in ids and gone not in indexers
        assert ids == ({secondary} if forgotten == "primary" else {env.destination_id})
