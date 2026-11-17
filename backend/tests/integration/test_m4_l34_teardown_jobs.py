"""The removal and the detachment go through the queue (M4 L34, EF-EXP-031, EF-INT-017,
EF-EXP-041): the real `DetachOps`, the real handlers and the real job pools over a real instance
(only Kavita is simulated).

What the queue holds is a key of identifiers: no phrase, no password. A pause parks the job and
nothing is deleted until it ends. A job that died `running` goes back to the queue at the boot
and finishes what it began. An incomplete end is a failed job, said by its code, and the same
order given again is a new job that finishes it.
"""

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.export.application.detach import (
    DetachOrder,
    DetachRefusedError,
    FullDetacher,
    detach_job_key,
)
from shelfhold.export.application.intents import IntentJournal
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.application.remove_destination import (
    REMOVAL_JOB_KIND,
    RemovalMode,
    removal_phrase,
)
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import export, platform
from shelfhold.jobs.public import MAX_ATTEMPTS, JobPools
from shelfhold.jobs.window import DEFAULT_WINDOW
from shelfhold.ops.detach import DetachOps, _Busy, register_teardown_jobs
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.maintenance import MaintenanceOps
from shelfhold.ops.settle_hooks import settle_hooks
from tests.l50_env import Seeded, seeded
from tests.maintenance_env import principal

pytestmark = pytest.mark.req("EF-EXP-031", "EF-INT-017", "EF-EXP-041")

DELETE = RemovalMode.DELETE_FILES
PASSWORD = "-".join(["synthetic", "kavita", "pw", "0034"])  # no literal: gitleaks scans commits


class Killed(BaseException):
    """What a `kill -9` looks like from inside the process."""


class _Headless:
    """The account's door, as `HeadlessOps` gives it."""

    def __init__(self) -> None:
        self.passwords: list[str | None] = []
        self.revoked: list[str] = []

    def target_find_key(self, *, target: str, password: str | None) -> str | None:
        self.passwords.append(password)
        return "7" if password else None

    def target_revoke_key_id(self, *, target: str, password: str | None, key_id: str) -> str:
        self.passwords.append(password)
        self.revoked.append(key_id)
        return "revoked"


class _Rig:
    def __init__(self, env: Seeded, pools: JobPools, ops: DetachOps, headless: _Headless) -> None:
        self.env, self.pools, self.ops, self.headless = env, pools, ops, headless

    @property
    def comp(self) -> Any:
        return self.env.inst.comp

    def run(self) -> None:
        huey = self.pools.huey("io")
        while (task := huey.dequeue()) is not None:
            huey.execute(task)

    def boot(self) -> None:
        """What `Runtime` and `boot_jobs` do at start, in their order: the open intents settled,
        the resource locks and slots released, the dead `running` jobs back in the queue."""
        comp, engine = self.comp, self.env.inst.engine
        with comp.write_context() as w:
            hook = settle_hooks(
                engine, w.fsops, w.loaded.locate, data_dir=comp.settings.data_dir,
                clock=comp.clock, bus=comp.bus,
            )  # fmt: skip
            IntentJournal(engine, w.fsops, clock=comp.clock).resolve_open(on_done=hook)
        ResourceLocks(engine, clock=comp.clock).release_all()
        self.pools.flush_huey_locks()
        self.pools.slots.release_all()
        for pending in self.pools.ledger.requeue_running():
            self.pools.ledger.mark_dispatched(pending.job_id, self.pools.dispatch(pending))

    def job(self, job_id: str) -> Any:
        with self.env.inst.engine.connect() as conn:
            return conn.execute(
                sa.select(platform.job_view).where(platform.job_view.c.id == job_id)
            ).one()

    def count(self, table: sa.Table) -> int:
        with self.env.inst.engine.connect() as conn:
            return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one())

    def files(self) -> list[Path]:
        return self.env.exported_files()

    def phrase(self, mode: RemovalMode = DELETE) -> str:
        return removal_phrase(self.env.destination_id, mode)

    def queue_removal(self) -> str:
        return self.ops.remove_queue(
            destination_id=self.env.destination_id, mode=DELETE.value, typed=self.phrase(),
            principal=principal("admin", self.comp.clock.now()),
        )  # fmt: skip


@pytest.fixture
def rig(settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Rig]:
    monkeypatch.setattr(rj, "SCAN_QUIET_S", 0)
    with seeded(settings, tmp_path) as env:
        series = env.new_series()
        for number in ("1", "2", "3"):
            env.add_chapter(series, number)
        env.export()
        assert len(env.exported_files()) == 3
        comp = env.inst.comp
        data = tmp_path / "queue"
        data.mkdir()
        pools = JobPools(data, engine=comp.engine, clock=comp.clock, seed="l34")
        headless = _Headless()
        register_teardown_jobs(pools, comp, headless)
        comp.pools = lambda: pools
        ops = DetachOps(comp, headless, flagged_names=lambda _id: 0)  # type: ignore[arg-type]
        yield _Rig(env, pools, ops, headless)


def test_the_removal_is_queued_with_identifiers_only_and_runs_to_the_end(rig: _Rig) -> None:
    job_id = rig.queue_removal()
    row = rig.job(job_id)
    assert row.kind == REMOVAL_JOB_KIND and row.status == "queued"
    assert rig.env.destination_id in row.dedupe_key and rig.phrase() not in str(tuple(row))
    assert len(rig.files()) == 3  # an order deletes nothing: the job does
    assert rig.queue_removal() == job_id  # the same order while it is active is the same job
    rig.run()
    done = rig.job(job_id)
    assert done.status == "succeeded" and done.error_code is None
    assert rig.files() == [] and rig.count(export.shelf_destination) == 0


def test_the_queue_respects_the_pause_nothing_is_deleted_until_it_ends(rig: _Rig) -> None:
    rig.pools.pause_writes("disk.low")
    job_id = rig.queue_removal()
    rig.run()
    assert rig.job(job_id).status == "paused"
    assert len(rig.files()) == 3 and rig.count(export.shelf_destination) == 1
    rig.pools.resume_writes("disk.low")
    rig.run()
    assert rig.job(job_id).status == "succeeded" and rig.files() == []


def test_a_job_that_died_running_goes_back_to_the_queue_and_finishes_without_a_double_delete(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    unlinked: list[str] = []
    real = Fsops.purge_unlink

    def die_after_the_second(self: Fsops, local: str, *a: Any, **k: Any) -> Any:
        result = real(self, local, *a, **k)
        unlinked.append(local)
        if len(unlinked) == 2:
            raise Killed
        return result

    monkeypatch.setattr(Fsops, "purge_unlink", die_after_the_second)
    job_id = rig.queue_removal()
    with pytest.raises(Killed):
        rig.run()
    monkeypatch.setattr(Fsops, "purge_unlink", real)
    assert rig.job(job_id).status == "running" and len(rig.files()) == 1
    rig.boot()
    rig.run()
    assert rig.job(job_id).status == "succeeded"
    assert rig.files() == [] and rig.count(export.shelf_destination) == 0
    assert len(unlinked) == len(set(unlinked))  # no file was deleted twice


def test_the_credential_is_never_in_the_ledger_and_a_restart_asks_for_it_again(
    rig: _Rig,
) -> None:
    target = _target_of(rig)
    job_id = rig.ops.full_queue(
        target_id=target, typed=f"detach {target}", credential=PASSWORD,
        discard_hidden_data=True, discard_tombstone_journal=True, key_gone=False,
        principal=principal("admin", rig.comp.clock.now()),
    )  # fmt: skip
    with rig.env.inst.engine.connect() as conn:
        everything = str([tuple(r) for r in conn.execute(sa.select(platform.job_view))])
    assert PASSWORD not in everything and f"detach {target}" not in everything
    rig.comp.credential_handoff.clear()  # the process restarted before the job ran
    rig.run()
    failed = rig.job(job_id)
    assert failed.status == "failed" and failed.error_code == "detach.incomplete"
    assert rig.headless.revoked == []  # no password, no revocation, and nothing guessed
    again = rig.ops.full_queue(
        target_id=target, typed=f"detach {target}", credential=PASSWORD,
        discard_hidden_data=True, discard_tombstone_journal=True, key_gone=False,
        principal=principal("admin", rig.comp.clock.now()),
    )  # fmt: skip
    assert again != job_id  # the first one ended: this is a new job, and it finishes the work
    rig.run()
    assert rig.job(again).status == "succeeded" and rig.headless.revoked == ["7"]
    assert PASSWORD not in str(rig.comp.credential_handoff)


def test_an_incomplete_removal_is_a_failed_job_said_by_its_code(rig: _Rig) -> None:
    victim = rig.files()[0]
    victim.write_bytes(b"edited by its owner after the export")
    job_id = rig.queue_removal()
    rig.run()
    row = rig.job(job_id)
    assert row.status == "failed" and row.error_code == "removal.incomplete"
    assert victim.read_bytes() == b"edited by its owner after the export"
    assert len(rig.files()) == 1 and rig.count(export.shelf_destination) == 1


def test_the_door_refuses_before_queueing(rig: _Rig) -> None:
    now = rig.comp.clock.now()
    for who in (principal(kind, now) for kind in ("key", "member", "child", "stale")):
        with pytest.raises(CodedRefusal):
            rig.ops.remove_queue(
                destination_id=rig.env.destination_id,
                mode=DELETE.value,
                typed=rig.phrase(),
                principal=who,
            )
    with pytest.raises(CodedRefusal):
        rig.ops.remove_queue(
            destination_id=rig.env.destination_id,
            mode=DELETE.value,
            typed="remove",
            principal=principal("admin", rig.comp.clock.now()),
        )
    with pytest.raises(CodedRefusal):  # IDOR: an identifier that is not a destination
        rig.ops.remove_queue(
            destination_id="01JNOSUCHDEST0000000000000",
            mode=DELETE.value,
            typed="x",
            principal=principal("admin", rig.comp.clock.now()),
        )
    assert rig.count(platform.job_view) == 0 and len(rig.files()) == 3


def test_the_retry_of_a_dead_removal_job_names_its_reserved_action(rig: _Rig) -> None:
    from shelfhold.ops.services import OpsServices

    job_id = rig.queue_removal()
    services = OpsServices.__new__(OpsServices)
    services._engine = rig.env.inst.engine  # type: ignore[attr-defined]
    assert services.jobs_retry_reserved_action(job_id) == "destination.remove_all"
    assert services.jobs_retry_reserved_action("01JNOSUCHJOB000000000000A") is None


def test_the_replay_of_a_cancelled_removal_needs_the_fresh_session_of_the_order(
    rig: _Rig,
) -> None:
    queue = MaintenanceOps(
        rig.env.inst.engine, rig.comp.clock, window=DEFAULT_WINDOW, control=rig.pools.control
    )
    job_id = rig.queue_removal()
    queue.cancel(job_id, principal=principal("admin", rig.comp.clock.now()))
    assert rig.job(job_id).status == "cancelled"
    for who in ("key", "member", "child", "stale"):
        with pytest.raises(CodedRefusal):
            queue.replay(job_id, principal=principal(who, rig.comp.clock.now()))
    assert rig.job(job_id).status == "cancelled"
    queue.replay(job_id, principal=principal("admin", rig.comp.clock.now()))  # a fresh session
    assert rig.job(job_id).status == "queued"  # back in the queue; the scheduler sends it again


def _queue_detach(rig: _Rig) -> str:
    target = _target_of(rig)
    return rig.ops.full_queue(
        target_id=target, typed=f"detach {target}", credential=PASSWORD,
        discard_hidden_data=True, discard_tombstone_journal=True, key_gone=False,
        principal=principal("admin", rig.comp.clock.now()),
    )  # fmt: skip


def _detach_key(rig: _Rig) -> str:
    return detach_job_key(
        DetachOrder(_target_of(rig), discard_hidden_data=True, discard_tombstone_journal=True)
    )


def test_a_second_order_does_not_keep_a_password_the_running_job_already_took(
    rig: _Rig,
) -> None:
    key = _detach_key(rig)
    first = _queue_detach(rig)
    handoff = rig.comp.credential_handoff
    assert handoff.holds(key)  # waiting for the job
    assert _queue_detach(rig) == first and handoff.holds(key)  # not taken yet: still the one
    assert handoff.take(key) == PASSWORD  # the job starts and takes it
    assert _queue_detach(rig) == first  # the same order again, the job is in flight
    assert not handoff.holds(key)  # nothing is left in memory for a run that will not come


def test_a_dead_letter_does_not_keep_the_password_after_repeated_busy(rig: _Rig) -> None:
    key = _detach_key(rig)
    handoff = rig.comp.credential_handoff
    busy = DetachRefusedError("busy")

    class _Context:
        job_id, dedupe_key = "01JJOB0000000000000000000A", key

        def __init__(self, attempts: int) -> None:
            self.ledger = SimpleNamespace(attempts_of=lambda _id: attempts)

        def record(self, **_counters: object) -> None:
            return None

    for attempts, kept in ((1, True), (MAX_ATTEMPTS - 1, True), (MAX_ATTEMPTS, False)):
        handoff.put(key, PASSWORD)
        with (
            patch.object(FullDetacher, "execute", side_effect=busy),
            pytest.raises(_Busy),
        ):
            rig.ops.run_detach_job(_Context(attempts))  # type: ignore[arg-type]
        assert handoff.holds(key) is kept, attempts  # the last attempt keeps nothing
        handoff.clear()


def test_a_submit_that_fails_after_the_password_was_put_leaves_nothing_in_memory(
    rig: _Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = _detach_key(rig)

    def broken(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("the ledger is not there")

    monkeypatch.setattr(rig.pools, "submit", broken)
    with pytest.raises(RuntimeError):
        _queue_detach(rig)
    assert not rig.comp.credential_handoff.holds(key)
    assert rig.count(platform.job_view) == 0


def _target_of(rig: _Rig) -> str:
    with rig.env.inst.engine.connect() as conn:
        return str(conn.execute(sa.select(export.export_target.c.id)).scalar_one())
