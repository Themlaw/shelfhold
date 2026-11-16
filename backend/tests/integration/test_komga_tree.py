"""EF-EXP-020, EF-EXP-032, EF-EXP-047 (M4 L08a): Komga and Kavita read the same tree (ADR-0052).

The mechanics are those of L06a: one primary writes, the other server is a secondary indexer that
copies the state of the primary in the same transaction and never writes; a tree is one copy; a
server indexes a tree once; and a shelf below 18+ is never bound to a Komga that nobody audited.
"""

from __future__ import annotations

import pytest

from shelfhold.export.domain.bindings import TreeOverlapError, UnpartitionedDestinationError
from shelfhold.export.targets.kavita import KavitaDestination
from shelfhold.export.targets.komga import KomgaDestination
from tests.export_env import export_env
from tests.family_env import family_env
from tests.fan_out_env import add_target, bind, records_of, service, tree_of
from tests.integration.test_fan_out import destination_replicas, folder, line, run


@pytest.mark.req("EF-EXP-020", "EF-EXP-032", "EF-EXP-047")
def test_kavita_and_komga_same_tree_per_adr() -> None:
    assert KavitaDestination.capabilities.indexes_tree is True
    assert KomgaDestination.capabilities.indexes_tree is True  # both may index a tree
    with export_env() as env:
        series = env.new_series()
        first_unit = env.add_chapter(series, "1")
        assert run(env, env.destination_id) == 1  # Kavita (the primary) writes the tree
        service(env).list_bindings(env.shelf_id)  # the tree of a binding made before L06a
        tree_id = tree_of(env, env.destination_id)
        assert tree_id is not None

        komga_target = add_target(env, kind="komga", partitioning="none", name="Komga")
        komga = bind(env, "Kavita", target_id=komga_target, attach_to_tree=tree_id, link=True)
        roles = {b.destination_id: b.role for b in service(env).list_bindings(env.shelf_id)}
        assert roles == {env.destination_id: "primary", komga: "secondary"}
        assert tree_of(env, komga) == tree_id  # the folder Komga reads is the one Kavita reads
        primary_lines = [line(r) for r in records_of(env, env.destination_id)]
        assert [line(r) for r in records_of(env, komga)] == primary_lines  # T9: one file state

        env.add_chapter(series, "2")
        assert run(env, komga) == 0  # the indexer never writes into the tree (T3)
        assert run(env, env.destination_id) == 1
        assert len(folder(env, "Kavita")) == 2
        assert [line(r) for r in records_of(env, komga)] == [
            line(r) for r in records_of(env, env.destination_id)
        ]
        assert destination_replicas(env, first_unit) == 1  # T11: one tree, one copy, two readers

        # a server indexes a tree once; a second Komga on the same folder is refused (T9)
        with pytest.raises(TreeOverlapError):
            bind(env, "Kavita", target_id=komga_target, attach_to_tree=tree_id)
        # without the explicit choice the same folder is an overlap, for Komga as for any server
        with pytest.raises(TreeOverlapError):
            bind(env, "Kavita", target_id=add_target(env, kind="komga", partitioning="none"))


@pytest.mark.req("EF-EXP-020", "EF-EXP-047")
def test_komga_can_be_the_primary_and_kavita_its_secondary() -> None:
    with export_env() as env:
        komga_target = add_target(env, kind="komga", partitioning="none", name="Komga")
        primary = bind(env, "Komga", target_id=komga_target)
        tree_id = tree_of(env, primary)
        secondary = bind(env, "Komga", target_id=add_target(env), attach_to_tree=tree_id)
        roles = {b.destination_id: b.role for b in service(env).list_bindings(env.shelf_id)}
        assert roles[primary] == "primary" and roles[secondary] == "secondary"
        assert run(env, secondary) == 0  # whichever the primary, the indexer writes nothing


@pytest.mark.req("EF-EXP-047", "EF-USR-024")
def test_komga_is_refused_a_shelf_below_18_until_its_accounts_are_audited() -> None:
    """Komga declares no partitioning in this lot: the barrier of the shelf holds, for the binding
    itself and for a tree it indexes (L08b raises the declaration with its audit)."""
    with family_env() as env:  # a shelf below 18+
        komga = add_target(env, kind="komga", partitioning="none", name="Komga")
        with pytest.raises(UnpartitionedDestinationError):
            bind(env, "Komga", target_id=komga)
        # even an admin's attestation of `library` is not enough before the audit exists: the
        # declaration of the adapter is `none`, the stored value of a first-party kind is not read
        attested = add_target(env, kind="komga", partitioning="library", name="Komga attested")
        with pytest.raises(UnpartitionedDestinationError):
            bind(env, "Komga attested", target_id=attested)
