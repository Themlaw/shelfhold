"""EF-EXP-041, EF-EXP-002 (M4 L06a): a pass over several bindings, a real process killed by SIGKILL
after every step, then a restart. The boot settles the journal, the pass runs again: 0 duplicate,
0 loss, no stray temporary file, no intent left open, and the manifest of every tree (with the
copy of its indexer) says what the disk holds."""

import json
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.infrastructure.schema import coherence
from tests.export_env import export_env
from tests.fan_out_env import bind, records_of, service
from tests.integration.test_fan_out_plans import digest, item_of, setup, write_plan

BACKEND = Path(__file__).resolve().parents[2]
STEPS = 24  # more than the ticks of the pass: the last runs end normally

CHILD = textwrap.dedent(
    """
    import json, os, signal, sys
    from pathlib import Path
    from shelfhold.export.application import fan_out as fan_out_module
    from shelfhold.export.application.fan_out import FanOut
    from shelfhold.export.application.intents import IntentJournal
    from shelfhold.export.application.pipeline import ExportPipeline
    from shelfhold.export.domain.path_map import PathEntry, PathMap
    from shelfhold.export.domain.physical_roots import ExpectedRoot
    from shelfhold.infrastructure.clock import SystemClock
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.fsops import Fsops
    from tests.integration.test_fan_out_plans import Plans
    from tests.plan_executor_env import FixedContents, Probe

    spec = json.loads(Path(sys.argv[1]).read_text())
    kill_at = int(sys.argv[2])
    library = spec["library"]
    fsops = Fsops.open(
        PathMap((PathEntry(f"{library}/manga", "/manga"),)),
        [ExpectedRoot(spec["root_id"], library, spec["instance_id"], 1)],
    )
    fsops.boot()
    engine = create_db_engine(Path(spec["database"]))
    ticks = [0]

    def die_after(owner, name):
        real = getattr(owner, name)

        def wrapper(*args, **kwargs):
            if name == "record_applied" and ticks[0] == kill_at:
                os.kill(os.getpid(), signal.SIGKILL)  # the files are down, the manifest is not
            result = real(*args, **kwargs)
            if ticks[0] == kill_at:
                os.kill(os.getpid(), signal.SIGKILL)
            ticks[0] += 1
            return result

        setattr(owner, name, wrapper)

    journal = IntentJournal(engine, fsops)
    pipeline = ExportPipeline(
        engine, fsops, locate=lambda p: (spec["root_id"], os.path.relpath(p, library)),
        data_dir=Path(spec["data"]), clock=SystemClock(),
        parse=lambda name: None, parse_verified=True,
    )
    contents = FixedContents({k: bytes.fromhex(v) for k, v in spec["data_by_item"].items()})
    pipeline.use_plan_executor(contents=contents, probe=Probe(spec["fingerprint"]))
    executor_journal = pipeline._plan_executor._journal
    for owner, name in [
        (executor_journal, "record"), (executor_journal, "mark_applying"),
        (fsops, "write_atomic"), (fsops, "mkdir"), (executor_journal, "mark_done"),
        (fan_out_module, "record_applied"),
    ]:
        die_after(owner, name)
    plans = Plans(spec["plans"])
    FanOut(engine, pipeline, plans).run(spec["shelf_id"], spec["call_id"])
    """
)


@pytest.mark.req("EF-EXP-041", "EF-EXP-002")
@pytest.mark.parametrize("kill_at", range(STEPS + 1))
def test_kill9_in_a_pass_over_several_bindings_resumes_without_duplicate_or_loss(
    tmp_path: Path, kill_at: int
) -> None:
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
        plan_by_destination = {
            env.destination_id: write_plan(one, two),
            second: write_plan(two, one, folder="Series B"),
        }
        plans.by_destination = plan_by_destination
        spec = tmp_path / "spec.json"
        spec.write_text(
            json.dumps(
                {
                    "library": str(env.lib.library),
                    "root_id": env.lib.library_root,
                    "instance_id": env.lib.instance_id,
                    "database": str(env.engine.url.database),
                    "data": str(env.lib.data),
                    "shelf_id": env.shelf_id,
                    "call_id": "c1",
                    "fingerprint": _probe.current,
                    "data_by_item": {k: v.hex() for k, v in contents.data.items()},
                    "plans": plan_by_destination,
                }
            )
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", CHILD, str(spec), str(kill_at)],
            check=False, capture_output=True, cwd=BACKEND,
        )  # fmt: skip
        assert result.returncode in (-signal.SIGKILL, 0), result.stderr.decode()

        # the restart: the boot settles the journal first, then the pass runs again, twice
        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open()
        ResourceLocks(env.engine, clock=env.clock).release_all()  # the lock the dead run held
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        for _ in range(2):
            assert {o.status for o in fan.run(env.shelf_id, "c1").outcomes} <= {"done", "indexer"}

        for subpath, folder, items in (
            ("Kavita", "Series A", (one, two)),
            ("Kavita2", "Series B", (two, one)),
        ):
            names = sorted(p.name for p in (env.lib.manga / subpath / folder).iterdir())
            assert names == ["Volume 1.cbz", "Volume 2.cbz"], (subpath, names)
            for n, item in enumerate(items, 1):
                data = (env.lib.manga / subpath / folder / f"Volume {n}.cbz").read_bytes()
                assert data == contents.data[item]
        stray = [p for p in env.lib.library.rglob("*") if p.name.endswith((".tmp", ".part"))]
        assert stray == []
        with env.engine.connect() as connection:
            intents = list(connection.execute(sa.select(coherence.fs_intent)))
        assert {r.state for r in intents} <= {"done", "cancelled"}
        assert sorted(r.op for r in intents if r.state == "done").count("write") == 4  # once each

        first_lines = records_of(env, env.destination_id)
        second_lines = records_of(env, second)
        assert len(first_lines) == len(second_lines) == 2
        assert {r.file_hash for r in first_lines} == {
            digest(contents.data[one]),
            digest(contents.data[two]),
        }
        key = ("export_item_id", "rel_path", "file_hash", "size", "state")
        assert [tuple(getattr(r, k) for k in key) for r in records_of(env, secondary)] == [
            tuple(getattr(r, k) for k in key) for r in first_lines
        ]  # T9: whatever the moment of the kill, the indexer says what the primary says
