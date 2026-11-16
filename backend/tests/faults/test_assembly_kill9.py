"""EF-EXP-019 / EF-EXP-041 (M4 L10, intouchable): an assembly killed by SIGKILL after every step.

Whatever the instant of the kill, the boot settles the intent: the destination never serves both
the chapters and the volume that replaces them, no chapter file is lost (it is served or in the
bin, whole), the manifest says what the disk says, and the assembly asked again ends where an
uninterrupted one would, with one volume file and no double.
"""

import os
import signal
from collections.abc import Callable
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.assembly import AssemblyOutcome
from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import coherence, export
from tests.assembly_env import (
    Chapters,
    bin_files,
    digest,
    exported_chapters,
    request,
    service,
)
from tests.export_env import ExportEnv, export_env

pytestmark = pytest.mark.req("EF-EXP-019", "EF-EXP-041")

CHAPTERS = 3
# record (pending), applying, the volume written, each chapter binned, done with the manifest
STEPS: tuple[tuple[type, str], ...] = (
    (IntentJournal, "record"),
    (IntentJournal, "mark_applying"),
    (Fsops, "write_atomic"),
    *((Fsops, "recycle") for _ in range(CHAPTERS)),
    (IntentJournal, "mark_done"),
)
TICKS = 3 + CHAPTERS + 1  # the steps, counted by calls


def _die_after(kill_at: int) -> None:
    """In the child only: SIGKILL right after the `kill_at`-th call of a step of the protocol."""
    ticks = [0]
    for owner, name in {(o, n) for o, n in STEPS}:
        real: Callable[..., Any] = getattr(owner, name)

        def wrapper(*args: Any, __real: Callable[..., Any] = real, **kwargs: Any) -> Any:
            result = __real(*args, **kwargs)
            if ticks[0] == kill_at:
                os.kill(os.getpid(), signal.SIGKILL)
            ticks[0] += 1
            return result

        setattr(owner, name, wrapper)


def _killed_assembly(env: ExportEnv, chapters: Chapters, kill_at: int) -> None:
    env.engine.dispose()
    pid = os.fork()
    if pid == 0:  # the child: the assembly, killed after one step
        try:
            env.engine.dispose(close=False)
            _die_after(kill_at)
            service(env).assemble(request(env, chapters))
        finally:
            os._exit(0)  # only when no kill point was reached
    _, status = os.waitpid(pid, 0)
    assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL


def _boot(env: ExportEnv) -> Any:
    """The restart: the locks of the dead process go, then every open intent is settled first."""
    ResourceLocks(env.engine, clock=env.clock).release_all()
    return IntentJournal(env.engine, env.lib.fsops, clock=env.clock).resolve_open(
        on_done=service(env).settle_hook()
    )


def _no_loss(env: ExportEnv, chapters: Chapters) -> None:
    """Every chapter file is whole, served or in the bin; never both."""
    served = {p.read_bytes() for p in env.files()}
    binned = [p.read_bytes() for p in bin_files(env)]
    for body in chapters.old_bytes:
        assert (body in served) != (body in binned), "a chapter is lost or twice"


@pytest.mark.parametrize("kill_at", range(TICKS))
def test_assembly_kill9_resumes_without_double_file(kill_at: int) -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3, 5])
        _killed_assembly(env, chapters, kill_at)

        _boot(env)

        with env.engine.connect() as connection:
            assert open_intent_paths(connection) == frozenset()
        _no_loss(env, chapters)
        record = export.export_record
        for row in env.rows(record):  # the manifest says what the disk says
            if row.state == "written":
                assert digest(env.root / str(row.rel_path)) == row.file_hash
        # Asked again, the assembly ends where an uninterrupted one would.
        again = service(env).assemble(request(env, chapters))
        assert again.outcome in {AssemblyOutcome.ASSEMBLED, AssemblyOutcome.NOT_SERVED}
        (volume,) = env.files()  # one file where three were, never two volumes
        assert volume not in chapters.targets
        assert sorted(p.read_bytes() for p in bin_files(env)) == sorted(chapters.old_bytes)
        lines = env.rows(record)
        assert sorted(r.state for r in lines) == ["recycled"] * 3 + ["written"]
        with env.engine.connect() as connection:
            done = connection.execute(
                sa.select(sa.func.count())
                .select_from(coherence.fs_intent)
                .where(coherence.fs_intent.c.op == "assemble", coherence.fs_intent.c.state == "done")
            ).scalar_one()
            assemblies = connection.execute(
                sa.select(sa.func.count()).select_from(export.export_assembly)
            ).scalar_one()
        assert done == 1 and assemblies == 1  # once, whatever the instant of the kill
        for host in ("staging", "assemble"):  # nothing left half-way
            assert not any(
                p.is_file() for p in (env.lib.library / ".shelfhold" / host).rglob("*")
            )


def test_a_killed_assembly_never_leaves_a_volume_next_to_its_chapters_after_the_boot() -> None:
    """Killed with the volume written and the chapters still served (the state in which both are
    on the disk): the boot goes on and bins the chapters, it does not leave the two."""
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3, 5])
        _killed_assembly(env, chapters, kill_at=2)  # right after the volume was written
        assert len(env.files()) == CHAPTERS + 1

        report = _boot(env)

        assert len(report.replayed) + len(report.completed) == 1 and not report.drifted
        assert len(env.files()) == 1 and len(bin_files(env)) == CHAPTERS


def test_a_killed_assembly_before_the_first_write_is_closed_and_asked_again() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3, 5])
        _killed_assembly(env, chapters, kill_at=1)  # announced, nothing written

        report = _boot(env)

        assert len(report.abandoned) == 1
        assert env.files() == chapters.targets and bin_files(env) == []


def test_the_boot_replay_of_a_volume_someone_replaced_is_a_drift_never_a_crash() -> None:
    with export_env() as env:
        chapters = exported_chapters(env, [4, 3, 5])
        _killed_assembly(env, chapters, kill_at=2)
        (volume,) = [p for p in env.files() if p not in chapters.targets]
        volume.write_bytes(b"PK\x03\x04 rewritten by a third party")

        report = _boot(env)

        assert len(report.drifted) == 1
        assert all(t.read_bytes() for t in chapters.targets)  # nothing was touched, nothing lost
        again = _boot(env)  # and the next boot does not stop either
        assert [*again.refused, *again.drifted]
