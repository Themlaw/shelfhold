"""EF-BIB-045, EF-BIB-048 (M4 L06b, intouchable): the bytes of a pass are read from the registry of
copies, each binding writes a file of its own, and a real process killed by SIGKILL after every
step resumes without duplicate, loss or truncated file: two files from one canonical archive, each
its own inode, never one link under two names, and the manifest and the registry say what the disk
holds."""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.fan_out import FanOut, HostContentStore
from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.infrastructure.schema import coherence
from shelfhold.library.public import independent_copies
from tests.byte_source_env import unit_with_item
from tests.export_env import export_env
from tests.fan_out_env import bind, records_of
from tests.integration.test_fan_out_bytes import Registry, RegistryPlans
from tests.integration.test_fan_out_plans import digest, setup, write_plan

BACKEND = Path(__file__).resolve().parents[2]
STEPS = 20  # more than the ticks of the pass: the last runs end normally

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
    from shelfhold.export.application.fan_out import HostContentStore
    from shelfhold.export.targets.kavita.destination import KavitaDestination
    from tests.plan_executor_env import Probe

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
    host = HostContentStore(engine, capabilities=lambda _target: KavitaDestination.capabilities)
    pipeline.use_plan_executor(contents=host, probe=Probe(spec["fingerprint"]))
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


@pytest.mark.req("EF-BIB-045", "EF-BIB-048")
@pytest.mark.parametrize("kill_at", range(STEPS + 1))
def test_kill9_while_two_bindings_copy_one_archive_resumes_without_duplicate_or_loss(
    tmp_path: Path, kill_at: int
) -> None:
    with export_env() as env:
        _fan, plans, _contents, probe = setup(env)
        unit = unit_with_item(env, env.new_series())
        second = bind(env, "Kavita2")
        plan_by_destination = {
            env.destination_id: write_plan(unit.item_id, folder="Series A"),
            second: write_plan(unit.item_id, folder="Series B"),
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
                    "fingerprint": probe.current,
                    "plans": plan_by_destination,
                }
            )
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", CHILD, str(spec), str(kill_at)],
            check=False, capture_output=True, cwd=BACKEND,
        )  # fmt: skip
        assert result.returncode in (-signal.SIGKILL, 0), result.stderr.decode()

        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open()
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        # the restart reads the bytes from the registry of the host (the library copy is there),
        # never from a file the killed run left in the tree of the other binding
        registry = Registry(env, plans.capabilities(env.target_id))
        again = RegistryPlans(plan_by_destination, registry=registry)
        host = HostContentStore(env.engine, capabilities=registry)
        env.pipeline.use_plan_executor(contents=host, probe=probe)
        restart = FanOut(env.engine, env.pipeline, again, clock=env.clock)
        for _ in range(2):
            assert {o.status for o in restart.run(env.shelf_id, "c1").outcomes} <= {"done"}

        one = env.lib.manga / "Kavita" / "Series A" / "Volume 1.cbz"
        two = env.lib.manga / "Kavita2" / "Series B" / "Volume 1.cbz"
        assert [p.name for p in one.parent.iterdir()] == ["Volume 1.cbz"]
        assert [p.name for p in two.parent.iterdir()] == ["Volume 1.cbz"]
        assert one.read_bytes() == unit.data == two.read_bytes()
        assert (one.stat().st_dev, one.stat().st_ino) != (two.stat().st_dev, two.stat().st_ino)
        assert one.stat().st_nlink == two.stat().st_nlink == 1
        stray = [p for p in env.lib.library.rglob("*") if p.name.endswith((".tmp", ".part"))]
        assert stray == []
        with env.engine.connect() as connection:
            intents = list(connection.execute(sa.select(coherence.fs_intent)))
            copies = independent_copies(connection, unit.unit_id)
            held = connection.execute(
                sa.text(
                    "SELECT rel_path, file_hash, independent FROM replica "
                    "WHERE location = 'destination' ORDER BY rel_path"
                )
            ).all()
        assert {r.state for r in intents} <= {"done", "cancelled"}
        assert sorted(r.op for r in intents if r.state == "done").count("write") == 2
        assert [r.file_hash for r in records_of(env, env.destination_id)] == [digest(unit.data)]
        assert [r.file_hash for r in records_of(env, second)] == [digest(unit.data)]
        assert copies == 1  # the library copy; a file of a tree is never counted as a copy
        assert [(r.file_hash, r.independent) for r in held] == [(digest(unit.data), 0)] * len(held)
        assert len(held) <= 2
        assert os.path.exists(unit.source.local_path)
