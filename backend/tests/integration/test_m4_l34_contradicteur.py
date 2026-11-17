"""PR #334 (M4 L34), contradicteur: suppression et intégrité des données.

1. **PoC (xfail strict).** `jobs retry` d'une tâche de retrait ou de détachement demande l'action
   réservée de l'ordre (`destination.remove_all`, `detach.full`), mais `jobs replay` (la file à la
   main, `queue.control`) ne la demande pas : la fraîcheur de `queue.control` n'est exigée qu'en
   profil durci. Une session admin périmée remet en file une tâche de retrait que l'admin avait
   annulée, et les fichiers partent sans session fraîche ni phrase.
2. **Vrai `SIGKILL`.** La tâche de retrait est exécutée dans un processus fils (`fork`) qui se tue
   par `SIGKILL` après la k-ième étape durable (intention écrite, marquée, fichier supprimé,
   intention close). Le parent fait ce que fait le démarrage (remise en file des tâches `running`)
   et la tâche finit : rien n'est supprimé deux fois, aucune intention ne reste ouverte, et aucun
   fichier hors de la destination n'est touché.
"""

import os
import signal
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.application.locks import ResourceLocks
from shelfhold.export.domain.physical_roots import SENTINEL_FILE
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.infrastructure.schema import export
from shelfhold.jobs.window import DEFAULT_WINDOW
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.maintenance import MaintenanceOps
from shelfhold.ops.settle_hooks import settle_hooks
from tests.integration.test_m4_l34_teardown_jobs import _Rig, rig  # noqa: F401 (the fixture)
from tests.maintenance_env import principal

pytestmark = pytest.mark.req("EF-EXP-031", "EF-INT-017")


def _queue_ops(rig: _Rig) -> MaintenanceOps:  # noqa: F811
    return MaintenanceOps(
        rig.env.inst.engine, rig.comp.clock, window=DEFAULT_WINDOW, control=rig.pools.control
    )


@pytest.mark.xfail(
    strict=True,
    reason="#334 contradicteur: `jobs replay` (queue.control) brings back a removal job "
    "without the fresh session `jobs retry` asks for",
)
def test_poc_a_stale_session_replays_a_cancelled_removal_and_the_files_go(
    rig: _Rig,  # noqa: F811
) -> None:
    queue = _queue_ops(rig)
    job_id = rig.queue_removal()
    queue.cancel(job_id, principal=principal("admin", rig.comp.clock.now()))
    assert rig.job(job_id).status == "cancelled"  # the admin changed their mind
    rig.run()
    assert len(rig.files()) == 3
    # hours later, a session past its freshness window (or a hijacked one)
    with pytest.raises(CodedRefusal):
        queue.replay(job_id, principal=principal("stale", rig.comp.clock.now()))
    rig.run()
    assert len(rig.files()) == 3, "the files went without a fresh session nor a phrase"


# -- a real SIGKILL ------------------------------------------------------------------------------

_STEPS = (
    (IntentJournal, "record"),
    (IntentJournal, "mark_applying"),
    (Fsops, "purge_unlink"),
    (IntentJournal, "mark_done"),
)
KILL_POINTS = 3 * len(_STEPS)  # three files, four durable steps each, at least
PAST_THE_END = 24  # more than the run has steps (the emptied folders add some): no kill


def _others(root: Path, kavita: Path) -> dict[str, bytes]:
    """Every file under the library root that is not in the destination's tree (the sentinel
    aside: each lease bumps its generation)."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and kavita not in p.parents and p.name != SENTINEL_FILE
    }


def _child(rig: _Rig, kill_at: int) -> None:  # noqa: F811
    """In the forked child: run the queue, SIGKILL after the `kill_at`-th durable step."""
    try:
        rig.env.inst.engine.dispose(close=False)  # never share the parent's connections
        ticks = [0]

        def wrap(owner: Any, name: str) -> None:
            real = getattr(owner, name)

            def step(*args: Any, **kwargs: Any) -> Any:
                result = real(*args, **kwargs)
                if ticks[0] == kill_at:
                    os.kill(os.getpid(), signal.SIGKILL)
                ticks[0] += 1
                return result

            setattr(owner, name, step)

        for owner, name in _STEPS:
            wrap(owner, name)
        rig.run()
    except BaseException:  # the child reports by its exit code only
        os._exit(3)
    os._exit(0)


def _boot(rig: _Rig) -> None:  # noqa: F811
    """What `Runtime` and `boot_jobs` do at start, in their order: the open intents settled, the
    resource locks and slots released, the dead `running` jobs put back in the queue."""
    comp, engine = rig.comp, rig.env.inst.engine
    engine.dispose()  # fresh connections, as a new process has
    with comp.write_context() as w:
        hook = settle_hooks(
            engine, w.fsops, w.loaded.locate, data_dir=comp.settings.data_dir,
            clock=comp.clock, bus=comp.bus,
        )  # fmt: skip
        IntentJournal(engine, w.fsops, clock=comp.clock).resolve_open(on_done=hook)
    ResourceLocks(engine, clock=comp.clock).release_all()
    rig.pools.flush_huey_locks()
    rig.pools.slots.release_all()
    for pending in rig.pools.ledger.requeue_running():
        rig.pools.ledger.mark_dispatched(pending.job_id, rig.pools.dispatch(pending))


@pytest.mark.parametrize("kill_at", range(PAST_THE_END))
def test_sigkill_during_the_removal_job_resumes_without_loss(
    rig: _Rig,  # noqa: F811
    kill_at: int,
) -> None:
    library = rig.env.inst.library
    kavita = library / "Kavita"
    before = _others(library, kavita)
    exported = rig.files()
    job_id = rig.queue_removal()

    pid = os.fork()
    if pid == 0:  # pragma: no cover (the child)
        _child(rig, kill_at)
    _, status = os.waitpid(pid, 0)
    if os.WIFSIGNALED(status):
        assert os.WTERMSIG(status) == signal.SIGKILL, status
        assert rig.job(job_id).status == "running"  # it died running
    else:
        assert kill_at >= KILL_POINTS, f"no kill at step {kill_at}"  # the steps are all reached
        assert os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0, status

    _boot(rig)
    rig.run()

    done = rig.job(job_id)
    assert done.status == "succeeded", (done.status, done.error_code)
    assert rig.files() == [] and not any(p.exists() for p in exported)
    assert rig.count(export.shelf_destination) == 0
    with rig.env.inst.engine.connect() as conn:
        assert open_intent_paths(conn) == frozenset()
        assert (
            conn.execute(
                sa.select(sa.func.count())
                .select_from(export.export_record)
                .where(export.export_record.c.shelf_destination_id == rig.env.destination_id)
            ).scalar_one()
            == 0
        )
    assert _others(library, kavita) == before  # nothing outside the destination was touched
