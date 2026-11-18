"""EF-EXP-041, EF-EXP-001, J-REP-1: the kill -9 proof of the plan executor, restarted with a **new**
call id.

The export job gives every pass a fresh call id, so after a restart the pass that finishes the work
is never the call that began it. `test_plan_executor_kill9` restarts with the same `"c1"` and could
not see it. Here the real process is killed after every step, the boot settles the journal, and the
next pass (`"c2"`) finishes the plan: no duplicate, no loss, no refusal of its own files."""

import json
import signal
import subprocess
import sys

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.target_state import save_fingerprint
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import coherence
from tests.export_env import export_env
from tests.faults.test_plan_executor_kill9 import BACKEND, CHILD, MTIME, PLAN, STEPS
from tests.plan_executor_env import BOOK_A, BOOK_B, BYTES, FINGERPRINT, make_exec_env


@pytest.mark.req("EF-EXP-041", "EF-EXP-001")
@pytest.mark.parametrize("kill_at", range(STEPS + 1))
def test_kill9_then_a_pass_with_a_new_call_id_finishes_the_plan(kill_at: int) -> None:
    with export_env() as env:
        x = make_exec_env(env)
        with write_transaction(env.engine) as connection:
            save_fingerprint(connection, env.target_id, FINGERPRINT, None, env.clock.now())
        result = subprocess.run(  # noqa: S603
            [
                sys.executable, "-c", CHILD, str(env.lib.library), env.lib.library_root,
                env.lib.instance_id, str(env.engine.url.database), env.destination_id,
                env.target_id, str(kill_at), json.dumps(PLAN),
            ],
            check=False,
            capture_output=True,
            cwd=BACKEND,
        )  # fmt: skip
        if kill_at < STEPS:
            assert result.returncode == -signal.SIGKILL, result.stderr.decode()
        else:
            assert result.returncode == 0, result.stderr.decode()
        IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open()
        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        for call_id in ("c2", "c3"):  # the next pass, then another: nothing is done twice
            x.plan_for(call_id, BOOK_A, BOOK_B)
            x.executor.execute(x.binding, call_id, PLAN)

        series = env.root / "Series A"
        assert sorted(p.name for p in series.iterdir()) == ["Volume 2.cbz"]
        assert (series / "Volume 2.cbz").read_bytes() == BYTES[BOOK_B]
        assert int((series / "Volume 2.cbz").stat().st_mtime) == MTIME
        binned = list(env.lib.library.glob(".shelfhold/recycle/*/manga/Kavita/Series A/V1.cbz"))
        assert [p.read_bytes() for p in binned] == [BYTES[BOOK_A]]
        with env.engine.connect() as connection:
            rows = list(connection.execute(sa.select(coherence.fs_intent)))
        assert {r.state for r in rows} <= {"done", "cancelled"}
        done = sorted(r.op for r in rows if r.state == "done")
        assert done == ["recycle", "rename", "utime", "write", "write"]  # one per deed
