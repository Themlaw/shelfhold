"""M3 L13, EF-INT-017: pause and resume (global and per source), cancel, priority and replay of the
jobs, by API. Each guard has a test that tries to get round it: a job that arrives under a pause, a
cancel of a job that holds locks, a replay twice, a user priority against a security repair, and
every principal that must be refused."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, identity, platform
from shelfhold.interfaces.http_api.maintenance import build_maintenance_router
from shelfhold.interfaces.http_api.stats import require_admin
from shelfhold.jobs.control import (
    PAUSE_CODE,
    REPAIR_PRIORITY,
    SECURITY_REPAIR_KINDS,
    USER_CLASSES,
)
from shelfhold.jobs.huey_app import CLASS_PRIORITY, JobContext
from shelfhold.jobs.ledger import PREEMPTED
from shelfhold.jobs.mode import PARK_CODE
from shelfhold.ops.errors import (
    CodedRefusal,
    InvalidChoiceError,
    NotWaitingError,
    UnknownSubjectError,
)
from tests.maintenance_env import Env, env

pytestmark = pytest.mark.req("EF-INT-017")

COMMANDS = ["pause", "resume", "cancel", "priority", "replay"]
SAFEGUARD = "library.safeguard"


def call(e: Env, command: str, who: str = "admin", *, job: str = "", source: str | None = None):  # type: ignore[no-untyped-def]
    principal = e.principal(who)
    match command:
        case "pause":
            return e.ops.pause(source, principal=principal)
        case "resume":
            return e.ops.resume(source, principal=principal)
        case "cancel":
            return e.ops.cancel(job, principal=principal)
        case "priority":
            return e.ops.set_priority(job, "new", principal=principal)
        case "replay":
            return e.ops.replay(job, principal=principal)
    raise ValueError(command)


def fetch(e: Env, ran: list[str], kind: str = "fetch", pool: str = "io") -> None:
    e.pools.register(kind, pool=pool, handler=lambda c: ran.append(c.dedupe_key))


def status_of(e: Env, job_id: str) -> tuple[str, str | None]:
    row = e.job(job_id)
    return row.status, row.error_code


def test_queue_pause_resume_cancel_priority_replay(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        a = e.pools.submit("fetch", dedupe_key="a", source_key="src-a").job_id
        e.pools.submit("fetch", dedupe_key="b", source_key="src-b")
        # pause one source: its jobs wait, the other source's go on
        assert e.ops.pause("src-a", principal=e.principal()) == {"scope": "src-a", "parked": 1}
        e.run()
        assert ran == ["b"] and status_of(e, a) == ("paused", PAUSE_CODE)
        # a job that arrives under the pause waits without failing
        late = e.pools.submit("fetch", dedupe_key="late", source_key="src-a").job_id
        e.run()
        assert ran == ["b"] and status_of(e, late) == ("paused", PAUSE_CODE)
        assert e.job(late).attempts == 0
        # resume: they go back, the scan sends them, they run
        assert e.ops.resume("src-a", principal=e.principal()) == {"scope": "src-a", "resumed": 2}
        e.pools.tick()
        e.run()
        assert sorted(ran) == ["a", "b", "late"]
        # the whole queue
        e.ops.pause(None, principal=e.principal())
        c = e.pools.submit("fetch", dedupe_key="c", source_key="src-b").job_id
        e.run()
        assert "c" not in ran and status_of(e, c) == ("paused", PAUSE_CODE)
        e.ops.resume(None, principal=e.principal())
        e.pools.tick()
        e.run()
        assert "c" in ran
        # cancel a job that waits: it never runs, the Huey task that still exists is a no-op
        d = e.pools.submit("fetch", dedupe_key="d").job_id
        assert e.ops.cancel(d, principal=e.principal()) == {"id": d, "outcome": "cancelled"}
        e.run()
        assert "d" not in ran and status_of(e, d) == ("cancelled", None)
        # priority
        low = e.pools.submit("fetch", dedupe_key="low", priority_class="backfill").job_id
        mid = e.pools.submit("fetch", dedupe_key="mid", priority_class="request").job_id
        e.pools.submit("fetch", dedupe_key="top", priority_class="new")
        assert e.ops.set_priority(low, "new", principal=e.principal())["priority_class"] == "new"
        e.pools.tick()
        before = len(ran)
        e.run()
        assert ran[before:] == ["top", "low", "mid"]  # `low` was raised above `mid`
        assert e.job(mid).status == "succeeded"
        # replay a cancelled job
        assert e.ops.replay(d, principal=e.principal()) == {"id": d, "replayed": True}
        e.pools.tick()
        e.run()
        assert "d" in ran and status_of(e, d) == ("succeeded", None)


def test_pause_resume_touch_only_their_own_jobs(tmp_path: Path) -> None:
    """The window, the maintenance mode and the disk guard each park jobs under their own code: a
    resume of the queue gives back its own and nothing else."""
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        window_job = e.pools.submit("fetch", dedupe_key="w").job_id
        with e.engine.begin() as conn:
            conn.execute(
                sa.update(platform.job_view)
                .where(platform.job_view.c.id == window_job)
                .values(status="paused", error_code=PREEMPTED, cursor='{"next_index": 2}')
            )
        disk = e.pools.submit("fetch", dedupe_key="d").job_id
        e.pools.ledger.pause_one(disk, "disk.low")
        mode = e.pools.submit("fetch", dedupe_key="m").job_id
        e.pools.ledger.pause_one(mode, PARK_CODE)
        mine = e.pools.submit("fetch", dedupe_key="q").job_id
        e.ops.pause(None, principal=e.principal())
        assert status_of(e, mine) == ("paused", PAUSE_CODE)
        e.ops.resume(None, principal=e.principal())
        assert status_of(e, mine) == ("queued", None)
        assert status_of(e, window_job) == ("paused", PREEMPTED)  # the discriminant is kept
        assert status_of(e, disk) == ("paused", "disk.low")
        assert status_of(e, mode) == ("paused", PARK_CODE)


def test_a_source_resumed_under_a_global_pause_stays_paused(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        e.ops.pause("src-a", principal=e.principal())
        e.ops.pause(None, principal=e.principal())
        job = e.pools.submit("fetch", dedupe_key="a", source_key="src-a").job_id
        e.run()
        assert e.ops.resume("src-a", principal=e.principal())["resumed"] == 0
        e.run()
        assert ran == [] and status_of(e, job) == ("paused", PAUSE_CODE)
        e.ops.resume(None, principal=e.principal())  # the whole queue: every pause is lifted
        e.pools.tick()
        e.run()
        assert ran == ["a"]


def test_the_second_pause_keeps_the_first_date(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        e.ops.pause(None, principal=e.principal())
        (first,) = e.rows(platform.app_setting, platform.app_setting.c.key.like("queue.pause.%"))
        e.clock.advance(hours=1)
        e.ops.pause(None, principal=e.principal())
        (again,) = e.rows(platform.app_setting, platform.app_setting.c.key.like("queue.pause.%"))
        assert first.value == again.value


# -- cancel ---------------------------------------------------------------------------------


def test_cancelling_a_running_job_releases_its_locks_and_its_slot(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        e.pools.slots.declare("upstream", 1)
        seen: dict[str, object] = {}

        def work(context: JobContext) -> None:
            with e.engine.begin() as conn:  # the locks of a disk-writing job, under its own id
                conn.execute(
                    sa.insert(coherence.resource_lock).values(
                        resource="work:W", holder_job_id=context.job_id,
                        lease_until=e.clock.now() + timedelta(hours=1),
                    )
                )  # fmt: skip
            outcome = e.ops.cancel(context.job_id, principal=e.principal())
            seen["outcome"] = outcome["outcome"]
            seen["stop"] = context.stop_requested  # the worker is told, like SIGTERM
            raise RuntimeError("died holding its locks")  # the handler does not clean up

        e.pools.register("work", pool="io", handler=work)
        job = e.pools.submit("work", dedupe_key="w", limit_key="upstream").job_id
        e.run()
        assert seen == {"outcome": "cancelling", "stop": True}
        assert status_of(e, job) == ("cancelled", None)  # not failed, not retried
        assert e.job(job).finished_at is not None
        assert e.rows(coherence.resource_lock) == []  # the locks went with it
        assert e.pools.slots.in_use("upstream") == 0
        assert e.rows(platform.app_setting, platform.app_setting.c.key.like("job.cancel.%")) == []
        e.pools.tick()
        e.run()
        assert status_of(e, job) == ("cancelled", None)  # and it does not come back


def test_cancelling_between_two_steps_leaves_no_open_intent(tmp_path: Path) -> None:
    """A step is never interrupted: the stop is read between two steps, so every file operation the
    job started is settled when it ends (EF-EXP-041). The next step never starts."""
    with env(tmp_path) as e:
        done: list[int] = []

        def step(n: int):  # type: ignore[no-untyped-def]
            def run() -> None:
                intent = new_ulid()
                with e.engine.begin() as conn:
                    conn.execute(
                        sa.insert(coherence.fs_intent).values(
                            id=intent, op="rename", intent_schema_version="1", epoch=0,
                            state="applying", src_path=f"/a/{n}", dst_path=f"/b/{n}",
                        )
                    )  # fmt: skip
                    conn.execute(  # the step finishes what it began
                        sa.update(coherence.fs_intent)
                        .where(coherence.fs_intent.c.id == intent)
                        .values(state="done")
                    )
                done.append(n)
                if n == 1:
                    e.ops.cancel(job_id[0], principal=e.principal())

            return run

        job_id: list[str] = []

        def handler(context: JobContext) -> None:
            job_id.append(context.job_id)
            context.run_steps([step(1), step(2), step(3)])

        e.pools.register("steps", pool="io", handler=handler)
        job = e.pools.submit("steps", dedupe_key="s").job_id
        e.run()
        assert done == [1]  # step 2 and 3 never started
        assert status_of(e, job) == ("cancelled", None)
        open_states = [r.state for r in e.rows(coherence.fs_intent)]
        assert open_states == ["done"]


def test_cancelling_a_finished_job_is_refused_and_unknown_ids_are_unknown(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        job = e.pools.submit("fetch", dedupe_key="x").job_id
        e.run()
        with pytest.raises(NotWaitingError):
            e.ops.cancel(job, principal=e.principal())
        with pytest.raises(UnknownSubjectError):
            e.ops.cancel(new_ulid(), principal=e.principal())
        assert status_of(e, job) == ("succeeded", None)


def test_cancelling_a_running_job_twice_asks_once(tmp_path: Path) -> None:
    with env(tmp_path) as e:

        def work(context: JobContext) -> None:
            for _ in range(2):
                e.ops.cancel(context.job_id, principal=e.principal())

        e.pools.register("work", pool="io", handler=work)
        job = e.pools.submit("work", dedupe_key="w").job_id
        e.run()
        assert status_of(e, job) == ("cancelled", None)
        assert [r.action for r in e.rows(identity.audit)] == ["queue.control"] * 2


def test_a_preempted_window_job_keeps_its_discriminant_when_the_queue_is_touched(
    tmp_path: Path,
) -> None:
    """`window.preempted` says the job resumes by itself at the next window: pause, resume, a
    priority, a replay or the maintenance mode must not turn it into something that waits for a
    human, and the next window still finds it."""
    with env(tmp_path) as e:
        e.pools.register("night", pool="maint", handler=lambda c: None, windowed=True)
        job = e.pools.submit("night", dedupe_key="n").job_id
        e.pools.ledger.mark_preempted(job)
        e.pools.ledger.save_cursor(job, 4)
        call(e, "pause")
        call(e, "resume")
        with pytest.raises(NotWaitingError):  # it does not wait in the queue: nothing to rank
            call(e, "priority", job=job)
        assert call(e, "replay", job=job) == {"id": job, "replayed": False}
        e.ops.enter(principal=e.principal("cli"))
        e.pools.tick()
        e.ops.leave(principal=e.principal("cli"))
        e.pools.tick()
        assert status_of(e, job) == ("paused", PREEMPTED)
        assert e.job(job).cursor == '{"next_index": 4}'
        # tonight's occurrence takes it back with its cursor, as before this lot
        with e.engine.begin() as conn:
            created = e.pools.ledger.create(
                conn, kind="night", dedupe_key="n", window_end=e.clock.now() + timedelta(hours=4)
            )
        assert (created.job_id, created.resumed) == (job, True)
        assert status_of(e, job) == ("queued", None)


# -- replay ---------------------------------------------------------------------------------


def test_replaying_a_failed_or_cancelled_job_twice_is_one_job(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        failed = e.pools.submit("fetch", dedupe_key="f").job_id
        cancelled = e.pools.submit("fetch", dedupe_key="c").job_id
        e.ops.cancel(cancelled, principal=e.principal())
        with e.engine.begin() as conn:
            conn.execute(
                sa.update(platform.job_view)
                .where(platform.job_view.c.id == failed)
                .values(status="failed", error_code="job.failed", last_error="Boom", attempts=3)
            )
        for job in (failed, cancelled):
            first = e.ops.replay(job, principal=e.principal())
            again = e.ops.replay(job, principal=e.principal())
            assert first == {"id": job, "replayed": True}
            assert again == {"id": job, "replayed": False}  # already queued: nothing more
            assert status_of(e, job) == ("queued", None)
            assert e.job(job).attempts == 0 and e.job(job).last_error is None
        assert len(e.rows(platform.job_view)) == 2  # no third row came out of it
        e.pools.tick()
        e.run()
        assert sorted(ran) == ["c", "f"]  # each ran once
        with pytest.raises(NotWaitingError):  # it succeeded: there is nothing to replay
            e.ops.replay(failed, principal=e.principal())


def test_replaying_a_job_whose_key_is_held_by_another_returns_that_one(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        old = e.pools.submit("fetch", dedupe_key="same").job_id
        e.ops.cancel(old, principal=e.principal())
        newer = e.pools.submit("fetch", dedupe_key="same").job_id  # the same work, queued again
        assert newer != old
        assert e.ops.replay(old, principal=e.principal()) == {"id": newer, "replayed": False}
        assert status_of(e, old) == ("cancelled", None)  # still cancelled: no second active row
        active = [r for r in e.rows(platform.job_view) if r.status in ("queued", "running")]
        assert [r.id for r in active] == [newer]


def test_a_replay_of_a_dead_letter_is_the_same_row_with_a_clean_slate(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        e.pools.register("boom", pool="io", handler=lambda c: 1 / 0)
        job = e.pools.submit("boom", dedupe_key="b").job_id
        with e.engine.begin() as conn:
            e.pools.ledger.mark_dead_letter(conn, job, "ZeroDivisionError")
        assert e.ops.replay(job, principal=e.principal())["replayed"] is True
        row = e.job(job)
        assert (row.status, row.attempts, row.error_code, row.finished_at) == (
            "queued",
            0,
            None,
            None,
        )


# -- priority -------------------------------------------------------------------------------


def test_no_user_priority_reaches_a_security_repair() -> None:
    assert max(CLASS_PRIORITY.values()) < REPAIR_PRIORITY
    assert set(USER_CLASSES) == set(CLASS_PRIORITY)  # the three classes, no fourth to pick
    assert "library.safeguard" in SECURITY_REPAIR_KINDS


def test_a_user_job_never_runs_before_a_security_repair(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        fetch(e, ran, SAFEGUARD)
        for n in range(3):
            e.pools.submit("fetch", dedupe_key=f"user-{n}", priority_class="new")
        repair = e.pools.submit(SAFEGUARD, dedupe_key="repair", priority_class="backfill").job_id
        user = e.pools.submit("fetch", dedupe_key="raised", priority_class="backfill").job_id
        e.ops.set_priority(user, "new", principal=e.principal())  # the highest a user may give
        e.pools.tick()
        e.run()
        assert ran[0] == "repair"  # first, though queued last and in the lowest class
        assert status_of(e, repair) == ("succeeded", None)


def test_the_priority_of_a_security_repair_cannot_be_changed(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        for kind in sorted(SECURITY_REPAIR_KINDS):
            fetch(e, ran, kind)
            job = e.pools.submit(kind, dedupe_key=kind, priority_class="backfill").job_id
            for chosen in USER_CLASSES:  # lowering it is as refused as raising it
                with pytest.raises(CodedRefusal) as refused:
                    e.ops.set_priority(job, chosen, principal=e.principal())
                assert refused.value.code == "queue.priority_locked"
            assert e.job(job).priority_class == "backfill"


@pytest.mark.parametrize("wanted", ["repair", "security", "critical", "urgent", "", "NEW", "50"])
def test_a_class_that_is_not_one_of_the_three_is_refused(tmp_path: Path, wanted: str) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        job = e.pools.submit("fetch", dedupe_key="x", priority_class="backfill").job_id
        with pytest.raises(InvalidChoiceError):
            e.ops.set_priority(job, wanted, principal=e.principal())
        assert e.job(job).priority_class == "backfill"


def test_a_priority_only_applies_to_a_job_that_waits(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        job = e.pools.submit("fetch", dedupe_key="x").job_id
        e.run()
        with pytest.raises(NotWaitingError):
            e.ops.set_priority(job, "backfill", principal=e.principal())
        with pytest.raises(UnknownSubjectError):
            e.ops.set_priority(new_ulid(), "backfill", principal=e.principal())


def test_the_old_task_of_a_re_ranked_job_does_not_run_it_early(tmp_path: Path) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        high = e.pools.submit("fetch", dedupe_key="high", priority_class="request").job_id
        e.pools.submit("fetch", dedupe_key="mid", priority_class="request")
        e.ops.set_priority(high, "backfill", principal=e.principal())  # lowered below `mid`
        e.pools.tick()
        e.run()
        assert ran == ["mid", "high"]  # its first task, at the old class, was dropped


# -- who may --------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["member", "child", "key", "device"])
@pytest.mark.parametrize("command", COMMANDS)
def test_member_child_and_api_key_are_refused_and_nothing_changes(
    tmp_path: Path, who: str, command: str
) -> None:
    with env(tmp_path) as e:
        ran: list[str] = []
        fetch(e, ran)
        job = e.pools.submit("fetch", dedupe_key="x", source_key="s").job_id
        before = e.rows(platform.job_view)
        with pytest.raises(CodedRefusal) as refused:
            call(e, command, who, job=job, source=None)
        assert refused.value.code == "forbidden"
        assert e.rows(platform.job_view) == before
        assert e.rows(platform.app_setting) == []
        assert e.rows(identity.audit) == []


@pytest.mark.parametrize("who", ["admin", "stale", "cli"])  # reversible: no freshness needed
@pytest.mark.parametrize("command", ["pause", "resume"])
def test_an_admin_and_the_host_may_control_the_queue_with_an_audit_line(
    tmp_path: Path, who: str, command: str
) -> None:
    with env(tmp_path) as e:
        principal = e.principal(who)
        getattr(e.ops, command)(None, principal=principal)
        (line,) = e.rows(identity.audit)
        assert (line.action, line.target) == ("queue.control", f"{command}:all")
        assert line.actor_id == principal.actor_id


def test_every_mutating_queue_and_maintenance_route_is_reserved_and_the_read_is_admin_only() -> (
    None
):
    router = build_maintenance_router(lambda: None)  # type: ignore[arg-type, return-value]
    seen: dict[str, str] = {}
    for route in router.routes:
        dependencies = [d.call for d in route.dependant.dependencies]  # type: ignore[attr-defined]
        methods = route.methods  # type: ignore[attr-defined]
        if "POST" in methods:
            reserved = [getattr(d, "reserved_action", None) for d in dependencies]
            assert any(r is not None for r in reserved), route.path  # type: ignore[attr-defined]
            seen[route.path] = next(r.name for r in reserved if r is not None)  # type: ignore[attr-defined]
        else:
            assert require_admin in dependencies, route.path  # type: ignore[attr-defined]
    assert seen == {
        "/api/v1/maintenance/enter": "maintenance.enter",
        "/api/v1/maintenance/exit": "maintenance.exit",
        "/api/v1/queue/pause": "queue.control",
        "/api/v1/queue/resume": "queue.control",
        "/api/v1/jobs/{job_id}/cancel": "queue.control",
        "/api/v1/jobs/{job_id}/priority": "queue.control",
        "/api/v1/jobs/{job_id}/replay": "queue.control",
    }
