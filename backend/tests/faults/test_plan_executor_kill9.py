"""EF-EXP-041, EF-EXP-001: a destination plan of three operations, a real process killed by
SIGKILL after every step of the executor, then a restart. The boot settles what the journal holds,
the same call runs again: 0 duplicate, 0 loss, no stray temporary file, no intent left open."""

import json
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.target_state import save_fingerprint
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import coherence
from tests.export_env import export_env
from tests.plan_executor_env import BOOK_A, BOOK_B, BYTES, FINGERPRINT, make_exec_env

BACKEND = Path(__file__).resolve().parents[2]
STEPS = 9  # mkdir, then for each write: record, applying, the write itself, done
PLAN = {
    "ops": [
        {"op": "mkdir", "rel_path": "Series A"},
        {
            "op": "write",
            "rel_path": "Series A/Volume 1.cbz",
            "content_ref": {"kind": "export_item", "ref": BOOK_A},
        },
        {
            "op": "write",
            "rel_path": "Series A/Volume 2.cbz",
            "content_ref": {"kind": "export_item", "ref": BOOK_B},
        },
    ]
}

CHILD = textwrap.dedent(
    """
    import json, os, signal, sys
    from pathlib import Path
    from shelfhold.export.application.intents import IntentJournal
    from shelfhold.export.application.plan_executor import PlanBinding, PlanExecutor, PlannedLedger
    from shelfhold.export.domain.path_map import PathEntry, PathMap
    from shelfhold.export.domain.physical_roots import ExpectedRoot
    from shelfhold.export.targets.kavita.destination import KavitaDestination
    from shelfhold.infrastructure.db import create_db_engine
        from shelfhold.sdk.fsops import PlannedContent
    from tests.plan_executor_env import BOOK_A, BOOK_B, FINGERPRINT, FixedContents, Probe

    library, root_id, instance_id, database, destination, target, kill_at, plan = sys.argv[1:9]
    kill_at = int(kill_at)
    fsops = Fsops.open(
        PathMap((PathEntry(f"{library}/manga", "/manga"),)),
        [ExpectedRoot(root_id, library, instance_id, 1)],
    )
    fsops.boot()
    engine = create_db_engine(Path(database))
    journal = IntentJournal(engine, fsops)
    ticks = [0]

    def die_after(owner, name):
        real = getattr(owner, name)

        def wrapper(*args, **kwargs):
            result = real(*args, **kwargs)
            if ticks[0] == kill_at:
                os.kill(os.getpid(), signal.SIGKILL)
            ticks[0] += 1
            return result

        setattr(owner, name, wrapper)

    for owner, name in [
        (journal, "record"), (journal, "mark_applying"), (fsops, "write_atomic"),
        (fsops, "mkdir"), (journal, "mark_done"),
    ]:
        die_after(owner, name)

    ledger = PlannedLedger()
    ledger.plan("c1", destination, PlannedContent(
        planned_refs=frozenset({BOOK_A, BOOK_B}), egress="foyer"))
    executor = PlanExecutor(
        engine, fsops, journal, ledger=ledger, contents=FixedContents(),
        probe=Probe(FINGERPRINT),
    )
    executor.execute(
        PlanBinding(destination_id=destination, target_id=target,
                    capabilities=KavitaDestination.capabilities),
        "c1", json.loads(plan),
    )
    """
)


@pytest.mark.req("EF-EXP-041", "EF-EXP-001")
@pytest.mark.parametrize("kill_at", range(STEPS + 1))
def test_kill9_between_ops_leaves_a_resumable_intent(tmp_path: Path, kill_at: int) -> None:
    with export_env() as env:
        x = make_exec_env(env)
        with write_transaction(env.engine) as connection:
            save_fingerprint(connection, env.target_id, FINGERPRINT, None, env.clock.now())
        root = env.lib.library_root
        result = subprocess.run(  # noqa: S603
            [
                sys.executable, "-c", CHILD, str(env.lib.library), root, env.lib.instance_id,
                str(env.engine.url.database), env.destination_id, env.target_id,
                str(kill_at), json.dumps(PLAN),
            ],
            check=False,
            capture_output=True,
            cwd=BACKEND,
        )  # fmt: skip
        if kill_at < STEPS:
            assert result.returncode == -signal.SIGKILL, result.stderr.decode()
        else:
            assert result.returncode == 0, result.stderr.decode()

        # the restart: the boot settles the journal first, then the call runs again
        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open()
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        x.plan_for("c1", BOOK_A, BOOK_B)
        x.executor.execute(x.binding, "c1", PLAN)
        x.executor.execute(x.binding, "c1", PLAN)  # and once more: nothing is done twice

        series = env.root / "Series A"
        assert sorted(p.name for p in series.iterdir()) == ["Volume 1.cbz", "Volume 2.cbz"]
        assert (series / "Volume 1.cbz").read_bytes() == BYTES[BOOK_A]
        assert (series / "Volume 2.cbz").read_bytes() == BYTES[BOOK_B]
        with env.engine.connect() as connection:
            rows = list(connection.execute(sa.select(coherence.fs_intent)))
        assert {r.state for r in rows} <= {"done", "cancelled"}
        written = [r for r in rows if r.op == "write" and r.state == "done"]
        assert len(written) == 2  # one intent per file that was written, whatever the kill
