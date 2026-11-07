"""M1b L02 review: the scheduler follows every freeze, the repair respects it (EF-INT-026)."""

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.freeze import FreezeState
from shelfhold.jobs.scheduler import Scheduler
from tests.fakes_clock import ManualClock
from tests.intents_env import NOW
from tests.restore_env import restore_env

pytestmark = pytest.mark.req("EF-INT-026")


class _Sink:
    def dispatch(self, job) -> str:
        return "t"


def _scheduler(env, clock):
    scheduler = Scheduler(env.engine, clock=clock, seed="s", dispatcher=_Sink())  # type: ignore[arg-type]
    return scheduler


@pytest.mark.req("EF-INT-026")
def test_review_a_process_freeze_alone_holds_the_scheduler_it_is_shared_with() -> None:
    with restore_env() as env:
        clock = ManualClock(NOW)
        shared = FreezeState()
        scheduler = Scheduler(env.engine, clock=clock, seed="s", dispatcher=_Sink(), freeze=shared)  # type: ignore[arg-type]
        scheduler.add("follow.cycle", scope_key="", cron="* * * * *")
        shared.freeze()  # `fsops.freeze()` alone: nothing in the database
        clock.advance(minutes=5)
        assert scheduler.held and scheduler.scan().queued == ()


@pytest.mark.req("EF-INT-026")
def test_review_a_locked_database_holds_the_task_instead_of_losing_it() -> None:
    with restore_env() as env:
        scheduler = _scheduler(env, ManualClock(NOW))

        def locked(_connection) -> None:
            raise sa.exc.OperationalError("SELECT", {}, Exception("database is locked"))

        sa.event.listen(env.engine, "before_cursor_execute", lambda *a: locked(None), named=False)
        assert scheduler.held  # held, hence retried: never run, never dropped
