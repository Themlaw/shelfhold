"""EF-EXP-022, EF-EXP-058 (M4 L09): a real process killed by SIGKILL after every step of a pass over
a CWA drop box, **between the write and the rename** included, resumes without duplicate, loss or
partial file: what the ingester can see in the folder is only ever a whole EPUB under its final
name, and each book comes out once, whatever the step."""

import json
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.fan_out import EpubFile
from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.infrastructure.schema import coherence
from tests.cwa_env import add_book, bind_cwa, folder, names, records, rig
from tests.export_env import export_env
from tests.fakes.cwa import FakeIngest

BACKEND = Path(__file__).resolve().parents[2]
STEPS = 14  # more than the ticks of the pass: the last runs end normally

CHILD = textwrap.dedent(
    """
    import json, os, signal, sys
    from pathlib import Path
    from shelfhold.export.application import fan_out as fan_out_module
    from shelfhold.export.application.fan_out import EpubFile, FanOut, HostContentStore
    from shelfhold.export.application.pipeline import ExportPipeline
    from shelfhold.export.domain.path_map import PathEntry, PathMap
    from shelfhold.export.domain.physical_roots import ExpectedRoot
    from shelfhold.export.targets.cwa.source import CwaSource
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
    store = {k: EpubFile(*v) for k, v in spec["epubs"].items()}
    source = CwaSource(engine)
    host = HostContentStore(
        engine, capabilities=source.capabilities, epub_path=lambda u: store.get(u)
    )
    source.attach(host)
    pipeline.use_plan_executor(contents=host, probe=Probe(None))
    journal = pipeline._plan_executor._journal
    for owner, name in [
        (journal, "record"), (journal, "mark_applying"), (fsops, "write_atomic"),
        (journal, "mark_done"), (fan_out_module, "record_applied"),
    ]:
        die_after(owner, name)
    FanOut(engine, pipeline, source).run(spec["shelf_id"], spec["call_id"])
    """
)


@pytest.mark.req("EF-EXP-022", "EF-EXP-058")
@pytest.mark.parametrize("kill_at", range(STEPS + 1))
def test_kill9_during_a_cwa_drop_resumes_without_duplicate_or_partial_file(
    tmp_path: Path, kill_at: int
) -> None:
    with export_env() as env:
        store: dict[str, EpubFile] = {}
        series = env.new_series("Synthetic Book")
        books = [add_book(env, series, n, store) for n in ("1", "2")]
        destination = bind_cwa(env)
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
                    "epubs": {k: [v.path, v.file_hash, v.content_id] for k, v in store.items()},
                }
            )
        )
        result = subprocess.run(  # noqa: S603
            [sys.executable, "-c", CHILD, str(spec), str(kill_at)],
            check=False, capture_output=True, cwd=BACKEND,
        )  # fmt: skip
        assert result.returncode in (-signal.SIGKILL, 0), result.stderr.decode()
        # whatever the step: the ingester sees nothing but whole EPUBs under their final names
        seen = FakeIngest(folder(env))
        for entry in folder(env).iterdir():
            assert entry.name.endswith(".epub") and not entry.name.startswith(".")
            assert entry.read_bytes() in {b.data for b in books}
        assert seen.violations == []

        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open()
        ResourceLocks(env.engine, clock=env.clock).release_all()  # the lock the dead run held
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        restart, _source = rig(env, store)
        for call in ("c1", "c2"):
            outcomes = restart.run(env.shelf_id, call).outcomes
            assert {o.status for o in outcomes if o.destination_id == destination} == {"done"}

        # a book may already have been ingested (and gone) when the process died: the intent that
        # says it was dropped keeps the restart from dropping it again
        taken = FakeIngest(folder(env))
        again = [i.data for i in taken.scan()]
        assert len(again) == len(set(again)) and set(again) <= {b.data for b in books}
        assert names(env) == []
        stray = [p for p in env.lib.library.rglob("*") if p.name.endswith((".tmp", ".part"))]
        assert stray == []
        with env.engine.connect() as connection:
            intents = list(connection.execute(sa.select(coherence.fs_intent)))
        assert {r.state for r in intents} <= {"done", "cancelled"}
        assert {r.export_item_id for r in records(env, destination)} == {b.item_id for b in books}
        assert all(os.path.exists(b.epub.path) for b in books)  # the library is intact
