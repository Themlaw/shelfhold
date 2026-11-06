"""ENF-QUAL-006: I9 (nothing under an open intent is re-planned) and I10 (one disk operation per
Work and per series path; a stale plan is never applied)."""

import tempfile
from contextlib import ExitStack
from pathlib import Path

import pytest
import sqlalchemy as sa
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from shelfhold.export.application.intents import IntentJournal, open_intent_paths, within_any
from shelfhold.export.application.locks import LockBusyError, ResourceLocks
from shelfhold.export.application.plans import PlanStore, StalePlanError
from shelfhold.infrastructure.schema import coherence
from tests.intents_env import make_intent_env

NAMES = st.sampled_from(["a", "b", "c", "d"]).map(lambda n: f"/library/manga/S/{n}.cbz")
OPEN_OR_NOT = st.sampled_from(["pending", "applying", "done", "cancelled", "stale_from_backup"])
JOBS = ["01JBZ3X5QH8M2N4P6R7S9T0V1A", "01JBZ3X5QH8M2N4P6R7S9T0V1B", "01JBZ3X5QH8M2N4P6R7S9T0V1C"]
SLOW = settings(max_examples=30, deadline=None, suppress_health_check=list(HealthCheck))


@pytest.mark.req("ENF-QUAL-006", "EF-EXP-041")
@given(
    intents=st.lists(st.tuples(NAMES, NAMES, OPEN_OR_NOT), max_size=6),
    candidates=st.lists(NAMES, max_size=6),
)
@SLOW
def test_i9_no_candidate_under_an_open_intent_survives(
    intents: list[tuple[str, str, str]], candidates: list[str]
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ienv = make_intent_env(Path(tmp))
        journal = IntentJournal(ienv.engine, ienv.fs.fsops, clock=ienv.clock)
        for src, dst, state in intents:
            ident = journal.record(op="rename", src=src, dst=dst)
            if state != "pending":
                with ienv.engine.begin() as conn:  # any state a crash can leave
                    conn.execute(
                        sa.update(coherence.fs_intent)
                        .where(coherence.fs_intent.c.id == ident)
                        .values(state=state)
                    )
        with ienv.engine.connect() as conn:
            covered = open_intent_paths(conn)
            kept = [p for p in candidates if not within_any(p, covered)]
        expected_open = {
            p for s, d, st_ in intents if st_ in ("pending", "applying") for p in (s, d)
        }
        assert covered == expected_open
        assert all(path not in covered for path in kept)
        assert kept == [c for c in candidates if c not in expected_open]
        ienv.engine.dispose()


@pytest.mark.req("ENF-QUAL-006", "EF-EXP-042")
@given(
    asks=st.lists(
        st.tuples(
            st.sampled_from(JOBS), st.sampled_from(["w1", "w2"]), st.sampled_from(["d/s1", "d/s2"])
        ),
        min_size=1, max_size=8,
    )
)  # fmt: skip
@SLOW
def test_i10_never_two_holders_of_one_resource(asks: list[tuple[str, str, str]]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ienv = make_intent_env(Path(tmp))
        locks = ResourceLocks(ienv.engine, clock=ienv.clock)
        held: list[tuple[str, str, str]] = []
        stack = ExitStack()  # alive: a collected context would release its locks
        for job, work, series in asks:
            if any(h[0] == job for h in held):
                continue  # a job holds one pair at a time
            clash = any(h[1] == work or h[2] == series for h in held)
            try:
                stack.enter_context(locks.hold(job, work=work, series=series, timeout=0))
            except LockBusyError:
                assert clash  # refused only when it really overlaps
            else:
                assert not clash  # granted only when nothing overlaps
                held.append((job, work, series))
        with ienv.engine.connect() as conn:
            rows = conn.execute(sa.text("SELECT resource, holder_job_id FROM resource_lock")).all()
        assert len({r.resource for r in rows}) == len(rows)  # one holder per resource
        stack.close()
        ienv.engine.dispose()


@pytest.mark.req("ENF-QUAL-006", "EF-EXP-042")
@given(base=st.integers(0, 50), now=st.integers(0, 50), if_match=st.integers(0, 50))
@SLOW
def test_i10_a_plan_whose_base_version_is_stale_is_never_applied(
    base: int, now: int, if_match: int
) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ienv = make_intent_env(Path(tmp))
        version = [base]
        store = PlanStore(ienv.engine, clock=ienv.clock, current_version=lambda: str(version[0]))
        plan = store.create(kind="rename_plan", files=[], ops=[])
        version[0] = now
        ran: list[object] = []
        if now == base == if_match:
            store.apply(plan.plan_id, if_match=str(if_match), run=ran.append)
        else:
            with pytest.raises(StalePlanError):
                store.apply(plan.plan_id, if_match=str(if_match), run=ran.append)
        assert ran == []
        ienv.engine.dispose()
