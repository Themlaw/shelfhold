"""EF-EXP-010, EF-BIB-045 (M4 L07, intouchable : intégrité): a real process killed by SIGKILL after
every step of a pass over a native folder resumes without duplicate, loss or truncated file: the
chapters, their ComicInfo and the cover come out once, whatever the step, and no `*.cbz` that is
visible is ever anything but a whole archive."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.public import RootGuard
from shelfhold.export.application.fan_out import FanOut, HostContentStore
from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.targets.native_folder.source import NativeFolderSource
from shelfhold.infrastructure.schema import coherence
from tests.byte_source_env import unit_with_item
from tests.export_env import export_env
from tests.fan_out_env import add_target, bind, records_of
from tests.plan_executor_env import Probe

BACKEND = Path(__file__).resolve().parents[2]
STEPS = 24  # more than the ticks of the pass: the last runs end normally

CHILD = textwrap.dedent(
    """
    import json, os, signal, sys
    from pathlib import Path
    from shelfhold.acquisition.public import RootGuard
    from shelfhold.export.application import fan_out as fan_out_module
    from shelfhold.export.application.fan_out import FanOut, HostContentStore
    from shelfhold.export.targets.native_folder.source import NativeFolderSource
    from shelfhold.export.application.pipeline import ExportPipeline
    from shelfhold.export.domain.path_map import PathEntry, PathMap
    from shelfhold.export.domain.physical_roots import ExpectedRoot
    from shelfhold.infrastructure.clock import SystemClock
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.fsops import Fsops
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

    pipeline = ExportPipeline(
        engine, fsops, locate=lambda p: (spec["root_id"], os.path.relpath(p, library)),
        data_dir=Path(spec["data"]), clock=SystemClock(),
        parse=lambda name: None, parse_verified=True,
    )
    source = NativeFolderSource(engine, root_check=RootGuard().check_root)
    host = HostContentStore(engine, capabilities=source.capabilities, renders=source.renders)
    source.attach(host)
    pipeline.use_plan_executor(contents=host, probe=Probe(None))
    journal = pipeline._plan_executor._journal
    for owner, name in [
        (journal, "record"), (journal, "mark_applying"), (fsops, "write_atomic"),
        (fsops, "mkdir"), (journal, "mark_done"), (fan_out_module, "record_applied"),
    ]:
        die_after(owner, name)
    FanOut(engine, pipeline, source).run(spec["shelf_id"], spec["call_id"])
    """
)

EXPECTED = [
    "Dr Stone/Ch.0001.cbz",
    "Dr Stone/Ch.0002.cbz",
    "Dr Stone/cover.png",
]


def _whole_archives(root: Path) -> None:
    for path in root.rglob("*.cbz"):
        with zipfile.ZipFile(path) as archive:  # a truncated file would not open
            assert archive.testzip() is None
            assert archive.namelist()[-1] == "ComicInfo.xml"


@pytest.mark.req("EF-EXP-010", "EF-BIB-045")
@pytest.mark.parametrize("kill_at", range(STEPS + 1))
def test_kill9_during_a_native_folder_pass_resumes_without_duplicate_or_loss(
    tmp_path: Path, kill_at: int
) -> None:
    with export_env() as env:
        series = env.new_series("Dr Stone")
        units = [unit_with_item(env, series, "1"), unit_with_item(env, series, "2")]
        destination = bind(
            env,
            "local",
            target_id=add_target(env, kind="native_folder", partitioning="none"),
            link=False,
        )
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
                }
            )
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", CHILD, str(spec), str(kill_at)],
            check=False, capture_output=True, cwd=BACKEND,
        )  # fmt: skip
        assert result.returncode in (-signal.SIGKILL, 0), result.stderr.decode()
        root = env.lib.manga / "local"
        _whole_archives(root)  # whatever the step: nothing visible is half written

        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open()
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        source = NativeFolderSource(env.engine, root_check=RootGuard().check_root)
        host = HostContentStore(
            env.engine, capabilities=source.capabilities, renders=source.renders
        )
        source.attach(host)
        env.pipeline.use_plan_executor(contents=host, probe=Probe(None))
        restart = FanOut(env.engine, env.pipeline, source, clock=env.clock)
        for _ in range(2):
            outcomes = restart.run(env.shelf_id, "c1").outcomes
            assert {o.status for o in outcomes if o.destination_id == destination} == {"done"}

        found = sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())
        assert found == EXPECTED  # no duplicate, no loss, no temporary file, no json
        _whole_archives(root)
        assert [
            p.name for p in (root / "Dr Stone").iterdir() if p.name.endswith((".tmp", ".part"))
        ] == []
        stray = [p for p in env.lib.library.rglob("*") if p.name.endswith((".tmp", ".part"))]
        assert stray == []
        with env.engine.connect() as connection:
            intents = list(connection.execute(sa.select(coherence.fs_intent)))
        assert {r.state for r in intents} <= {"done", "cancelled"}
        assert {r.export_item_id for r in records_of(env, destination)} == {
            u.item_id for u in units
        }
        assert all(os.path.exists(str(u.source.local_path)) for u in units)  # the library is intact
