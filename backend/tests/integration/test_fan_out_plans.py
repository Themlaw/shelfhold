"""EF-EXP-002, EF-EXP-047, EF-EXP-001 (M4 L06a, intouchable): a pass over the bindings of a shelf.

A destination describes a plan for each binding; the fan-out gives each one its own planned
content (the Editions admitted on the shelf of **that** binding, with the egress the instance
attested), runs it through the executor of L04, and keeps the manifest of each tree. One binding
that fails marks nothing on the others. The only fakes are the answers of the servers and the bytes
the host owns.
"""

import ast
import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application import fan_out as fan_out_module
from shelfhold.export.application.export_items import create_export_item
from shelfhold.export.application.fan_out import FanOut
from shelfhold.export.application.plan_executor import PlanBinding
from shelfhold.export.application.target_state import save_fingerprint
from shelfhold.export.targets.kavita.destination import KavitaDestination
from shelfhold.infrastructure.db import write_transaction
from shelfhold.sdk.api_call import ApiCall
from shelfhold.sdk.export_target import TargetCapabilities
from tests.export_env import ExportEnv, export_env
from tests.family_env import family_env, rate
from tests.fan_out_env import bind, records_of, service
from tests.plan_executor_env import FINGERPRINT, FixedContents, Probe

SRC = Path(fan_out_module.__file__).resolve().parents[2]


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class Plans:
    """What the destinations answer: a plan per binding, the same capabilities for all."""

    by_destination: dict[str, object] = field(default_factory=dict)

    def capabilities(self, _target_id: str) -> TargetCapabilities:
        return KavitaDestination.capabilities

    def plan(self, binding: PlanBinding) -> object | None:
        return self.by_destination.get(binding.destination_id)

    def calls(self, _binding: PlanBinding) -> tuple[object, ...]:
        return ()

    def sender(self, _binding: PlanBinding) -> Callable[[ApiCall], object]:
        raise AssertionError("this test sends no call")


def write_plan(*items: str, folder: str = "Series A") -> dict[str, Any]:
    return {
        "ops": [
            {"op": "mkdir", "rel_path": folder},
            *(
                {
                    "op": "write",
                    "rel_path": f"{folder}/Volume {n}.cbz",
                    "content_ref": {"kind": "export_item", "ref": item},
                }
                for n, item in enumerate(items, 1)
            ),
        ]
    }


def item_of(env: ExportEnv, series: Any, number: str, data: bytes, contents: FixedContents) -> str:
    unit = env.add_chapter(series, number)
    with write_transaction(env.engine) as connection:
        item = create_export_item(
            connection, edition_id=series.edition_id, unit_id=unit, kind="unit", frozen_attrs={}
        )
    contents.data[item] = data
    return item


def setup(env: ExportEnv) -> tuple[FanOut, Plans, FixedContents, Probe]:
    contents, probe, plans = FixedContents({}), Probe(FINGERPRINT), Plans()
    env.pipeline.use_plan_executor(contents=contents, probe=probe)
    with write_transaction(env.engine) as connection:
        save_fingerprint(connection, env.target_id, FINGERPRINT, None, env.clock.now())
    return FanOut(env.engine, env.pipeline, plans, clock=env.clock), plans, contents, probe


def folder(env: ExportEnv, subpath: str) -> list[str]:
    return sorted(
        str(p.relative_to(env.lib.manga / subpath))
        for p in (env.lib.manga / subpath).rglob("*")
        if p.is_file()
    )


@pytest.mark.req("EF-EXP-002", "EF-EXP-001")
def test_a_pass_runs_every_binding_and_a_failure_marks_nothing_on_the_others() -> None:
    with export_env() as env:
        fan, plans, contents, _probe = setup(env)
        series = env.new_series()
        one = item_of(env, series, "1", b"first volume " * 50, contents)
        two = item_of(env, series, "2", b"second volume " * 50, contents)
        second = bind(env, "Kavita2")
        tree_id = {b.destination_id: b.tree_id for b in service(env).list_bindings(env.shelf_id)}[
            env.destination_id
        ]
        secondary = bind(env, "Kavita", attach_to_tree=tree_id)
        plans.by_destination = {
            env.destination_id: write_plan(one, two),
            second: write_plan(one, "01HZZZZZZZZZZZZZZZZZZZZZZZ"),  # cites what nobody planned
            secondary: write_plan(one),  # an indexer is never asked to write
        }

        report = fan.run(env.shelf_id, "call-1")

        outcome = {o.destination_id: o for o in report.outcomes}
        assert outcome[env.destination_id].status == "done"
        assert outcome[env.destination_id].executed == 3  # a folder and two files
        assert outcome[second].status == "failed" and outcome[second].code == "export.plan_refused"
        assert outcome[secondary].status == "indexer"
        assert folder(env, "Kavita") == ["Series A/Volume 1.cbz", "Series A/Volume 2.cbz"]
        assert folder(env, "Kavita2") == []  # refused as a whole: not one operation ran
        primary = records_of(env, env.destination_id)
        assert {r.export_item_id for r in primary} == {one, two}
        assert {r.state for r in primary} == {"written"}
        assert records_of(env, second) == []  # the failure marked nothing, here or elsewhere
        copy = records_of(env, secondary)  # T9: the indexer holds the same file states
        key = ("export_item_id", "rel_path", "file_hash", "size", "state")
        assert [tuple(getattr(r, k) for k in key) for r in copy] == [
            tuple(getattr(r, k) for k in key) for r in primary
        ]
        assert {r.file_hash for r in primary} == {
            digest(contents.data[one]),
            digest(contents.data[two]),
        }

        # the same call again: nothing is done twice, no line is duplicated
        again = fan.run(env.shelf_id, "call-1")
        assert {o.destination_id: o.status for o in again.outcomes}[env.destination_id] == "done"
        assert len(folder(env, "Kavita")) == 2 and len(records_of(env, env.destination_id)) == 2


@pytest.mark.req("EF-EXP-002", "EF-EXP-001")
def test_a_server_replaced_pauses_its_binding_and_the_pass_goes_on() -> None:
    with export_env() as env:
        fan, plans, contents, probe = setup(env)
        series = env.new_series()
        one = item_of(env, series, "1", b"a volume " * 50, contents)
        second = bind(env, "Kavita2")
        plans.by_destination = {
            env.destination_id: write_plan(one),
            second: write_plan(one),
        }
        probe.current = "sha256:" + "b" * 64  # the server of the first binding is another one
        # the second target has no fingerprint yet: a first sight is not a change
        with env.engine.connect() as connection:
            second_target = connection.execute(
                sa.text("SELECT target_id FROM shelf_destination WHERE id = :d"), {"d": second}
            ).scalar_one()
        assert second_target != env.target_id

        report = fan.run(env.shelf_id, "call-2")

        statuses = {o.destination_id: (o.status, o.code) for o in report.outcomes}
        assert statuses[env.destination_id] == ("paused", "target.instance_changed")
        assert statuses[second][0] == "done"
        assert folder(env, "Kavita") == [] and folder(env, "Kavita2") == ["Series A/Volume 1.cbz"]


@pytest.mark.req("EF-EXP-047", "EF-EXP-002")
def test_family_planned_refs_are_the_ones_of_the_shelf_of_each_binding() -> None:
    """Point m5 of #299: what an adult shelf holds, or what is above the ceiling or unrated, is
    never planned for a binding of a family shelf, whatever a destination asks for."""
    with family_env(13) as env:
        fan, plans, contents, _probe = setup(env)
        second = bind(env, "Kavita2")
        mine = env.new_series("Mine")
        above = env.new_series("Above")
        unrated = env.new_series("Unrated")
        rate(env, mine, 10)
        rate(env, above, 18)
        kept = item_of(env, mine, "1", b"mine " * 50, contents)
        too_old = item_of(env, above, "1", b"above " * 50, contents)
        waiting = item_of(env, unrated, "1", b"unrated " * 50, contents)
        adult_shelf = "01HZZZZZZZZZZZZZZZZZZZZZZY"
        with write_transaction(env.engine) as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO shelf (id, name, phrase, max_age_rating) "
                    "VALUES (:i, 'Elsewhere', 'Elsewhere', 99)"
                ),
                {"i": adult_shelf},
            )
        elsewhere = env.new_series("Elsewhere", shelf_id=adult_shelf)
        rate(env, elsewhere, 18)
        adult_item = item_of(env, elsewhere, "1", b"adult " * 50, contents)

        for refused in (too_old, waiting, adult_item):
            plans.by_destination = {
                env.destination_id: write_plan(kept, refused),
                second: write_plan(refused),
            }
            report = fan.run(env.shelf_id, f"call-{refused[-4:]}")
            assert {o.status for o in report.outcomes} == {"failed"}, refused
            assert folder(env, "Kavita") == [] == folder(env, "Kavita2")  # not even `kept`
            assert records_of(env, env.destination_id) == records_of(env, second) == []

        plans.by_destination = {env.destination_id: write_plan(kept), second: write_plan(kept)}
        report = fan.run(env.shelf_id, "call-ok")
        assert {o.status for o in report.outcomes} == {"done"}
        assert folder(env, "Kavita") == ["Series A/Volume 1.cbz"] == folder(env, "Kavita2")


@pytest.mark.req("EF-EXP-047", "EF-EXP-002")
def test_a_binding_that_does_not_partition_is_refused_at_every_pass() -> None:
    with family_env(13) as env:
        fan, plans, contents, _probe = setup(env)
        series = env.new_series("Mine")
        rate(env, series, 10)
        kept = item_of(env, series, "1", b"mine " * 50, contents)
        from tests.fan_out_env import add_target, set_partitioning

        target = add_target(env, kind="plugin_x", partitioning="library")
        second = bind(env, "Kavita2", target_id=target)
        plans.by_destination = {env.destination_id: write_plan(kept), second: write_plan(kept)}
        set_partitioning(env, target, "library")  # the attestation is withdrawn
        report = fan.run(env.shelf_id, "call-3")
        statuses = {o.destination_id: (o.status, o.code) for o in report.outcomes}
        assert statuses[second] == ("refused", "shelf.destination_not_partitioned")
        assert statuses[env.destination_id][0] == "done"
        assert folder(env, "Kavita2") == []


@pytest.mark.req("EF-EXP-001", "EF-EXP-002")
def test_nothing_runs_a_plan_but_the_pipeline_door() -> None:
    """The composition root binds the executor, and the only callers are the pipeline's doors and
    the fan-out above them: the guards of L04 (gate, lease, instance fingerprint, tree role) are
    not walked around."""
    assert hasattr(fan_out_module, "FanOut")
    offenders: list[str] = []
    for path in sorted((SRC / "shelfhold").rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel.endswith(("export/application/plan_executor.py", "export/application/pipeline.py")):
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "attr", None) or getattr(node.func, "id", None)
                if name in {"PlanExecutor", "run_calls"}:
                    offenders.append(f"{rel}:{node.lineno}:{name}")
    assert offenders == []


@pytest.mark.req("EF-EXP-001", "EF-EXP-002")
def test_the_composition_root_binds_the_executor_with_the_host_bytes_and_the_server_identity() -> (
    None
):
    """`use_plan_executor` is called where the pipeline of production is built, with the host's
    own `ContentStore` and a probe that answers from the identity the server gave."""
    import shelfhold.runtime_jobs as jobs

    tree = ast.parse(Path(jobs.__file__).read_text())
    (build,) = [
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_pipeline"
    ]
    called = {
        getattr(n.func, "attr", getattr(n.func, "id", ""))
        for n in ast.walk(build)
        if isinstance(n, ast.Call)
    }
    assert {"use_plan_executor", "HostContentStore", "CachedInstanceProbe"} <= called
