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
STEPS = 21  # mkdir; per write, rename, utime, recycle: record, applying, the deed, done
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
        {
            "op": "rename",
            "rel_path": "Series A/Volume 1.cbz",
            "dst_rel_path": "Series A/V1.cbz",
        },
        {
            "op": "touch_mtime",
            "rel_path": "Series A/Volume 2.cbz",
            "mtime": "2020-01-02T03:04:05+00:00",
        },
        {"op": "recycle", "rel_path": "Series A/V1.cbz"},
    ]
}
MTIME = 1577934245

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
    from shelfhold.infrastructure.fsops import Fsops
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
        (fsops, "mkdir"), (fsops, "rename"), (fsops, "utime"), (fsops, "recycle"),
        (journal, "mark_done"),
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
        assert sorted(p.name for p in series.iterdir()) == ["Volume 2.cbz"]  # 1 was renamed, binned
        assert (series / "Volume 2.cbz").read_bytes() == BYTES[BOOK_B]
        assert int((series / "Volume 2.cbz").stat().st_mtime) == MTIME
        binned = list(env.lib.library.glob(".shelfhold/recycle/*/manga/Kavita/Series A/V1.cbz"))
        assert [p.read_bytes() for p in binned] == [BYTES[BOOK_A]]  # once, never twice
        with env.engine.connect() as connection:
            rows = list(connection.execute(sa.select(coherence.fs_intent)))
        assert {r.state for r in rows} <= {"done", "cancelled"}
        done = sorted(r.op for r in rows if r.state == "done")
        assert done == ["recycle", "rename", "utime", "write", "write"]  # one per deed


MID_WRITE = textwrap.dedent(
    """
    import os, signal
    from shelfhold.infrastructure.fsops.confined import ConfinedRoot

    real = ConfinedRoot.rename

    def killed(self, src, dst, *, replace, durable=True):
        os.kill(os.getpid(), signal.SIGKILL)  # the bytes are staged, the name is not there yet
        return real(self, src, dst, replace=replace, durable=durable)

    ConfinedRoot.rename = killed
    """
)


@pytest.mark.req("EF-EXP-041", "EF-EXP-001")
def test_kill9_during_a_write_leaves_no_partial_file_and_a_plan_that_resumes(
    tmp_path: Path,
) -> None:
    with export_env() as env:
        x = make_exec_env(env)
        with write_transaction(env.engine) as connection:
            save_fingerprint(connection, env.target_id, FINGERPRINT, None, env.clock.now())
        one_write = {"ops": PLAN["ops"][:2]}
        result = subprocess.run(  # noqa: S603
            [
                sys.executable, "-c", MID_WRITE + CHILD, str(env.lib.library),
                env.lib.library_root, env.lib.instance_id, str(env.engine.url.database),
                env.destination_id, env.target_id, "999", json.dumps(one_write),
            ],
            check=False,
            capture_output=True,
            cwd=BACKEND,
        )  # fmt: skip
        assert result.returncode == -signal.SIGKILL, result.stderr.decode()
        series = env.root / "Series A"
        assert not (series / "Volume 1.cbz").exists()  # never half a file under its name
        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open()
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        x.plan_for("c1", BOOK_A, BOOK_B)
        x.executor.execute(x.binding, "c1", one_write)
        assert (series / "Volume 1.cbz").read_bytes() == BYTES[BOOK_A]
