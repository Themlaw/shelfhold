"""kill -9 at each stage of the erasure of a root's reserved folder, the one deed of the full
detachment (M3 L30, EF-EXP-031, EF-EXP-041): the reserved folder and the sentinel are both there or
both gone, never a root half-erased and unmarked, and once the boot and `settle` have run no
intention is left open."""

import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from shelfhold.export.application.detach import DETACH_SCOPE, FullDetacher
from shelfhold.export.application.intents import IntentJournal, open_intent_paths
from shelfhold.export.domain.physical_roots import RESERVED_DIR, SENTINEL_FILE
from shelfhold.infrastructure.fsops import Fsops
from tests.intents_env import make_intent_env
from tests.purge_env import ADMIN

STAGES = 4  # record, mark_applying, erase, mark_done
TARGET = "01J0000000000000000000TGT0"

CHILD = textwrap.dedent(
    """
    import os, signal, sys
    from pathlib import Path
    from shelfhold.export.application.intents import IntentJournal
    from shelfhold.export.domain.path_map import PathEntry, PathMap
    from shelfhold.export.domain.physical_roots import ExpectedRoot
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.fsops import Fsops

    library, root_id, instance_id, database, kill_at, scope, target = sys.argv[1:8]
    kill_at = int(kill_at)
    fsops = Fsops.open(
        PathMap((PathEntry(f"{library}/manga", "/manga"),)),
        [ExpectedRoot(root_id, library, instance_id, 3)],
    )
    fsops.boot()
    journal = IntentJournal(create_db_engine(Path(database)), fsops)
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
        (journal, "record"), (journal, "mark_applying"), (fsops, "erase_host_area"),
        (journal, "mark_done"),
    ]:
        die_after(owner, name)

    intent = journal.record(
        op="delete", src=f"{library}/.shelfhold",
        extra={"scope": scope, "target_id": target, "root_id": ""},
    )
    journal.mark_applying(intent)
    fsops.erase_host_area(library)
    journal.mark_done(intent)
    """
)


@pytest.mark.req("EF-EXP-031", "EF-EXP-041")
@pytest.mark.parametrize("kill_at", range(STAGES + 1))
def test_kill9_at_each_stage_of_the_erasure_of_a_reserved_folder(
    tmp_path: Path, kill_at: int
) -> None:
    ienv = make_intent_env(tmp_path)
    env = ienv.fs
    root = env.expected[0]
    (env.library / RESERVED_DIR).mkdir(exist_ok=True)
    (env.library / RESERVED_DIR / "tombstones").write_bytes(b"journal")
    assert (env.library / SENTINEL_FILE).exists()
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CHILD, str(env.library), root.root_id, root.instance_id,
         str(tmp_path / "shelfhold.db"), str(kill_at), DETACH_SCOPE, TARGET],
        check=False,
        capture_output=True,
    )  # fmt: skip
    if kill_at < STAGES:
        assert result.returncode == -signal.SIGKILL, result.stderr.decode()
    else:
        assert result.returncode == 0, result.stderr.decode()

    fsops = Fsops.open(env.path_map, env.expected)
    fsops.boot()
    journal = IntentJournal(ienv.engine, fsops, clock=ienv.clock)
    journal.resolve_open()
    detacher = FullDetacher(
        ienv.engine, fsops, journal, clock=ienv.clock, roots=lambda _local: None
    )
    report = detacher.settle(TARGET, resume=False, principal=ADMIN)
    with ienv.engine.connect() as conn:
        assert open_intent_paths(conn) == frozenset()  # nothing is left half-done
    area, sentinel = (env.library / RESERVED_DIR).exists(), (env.library / SENTINEL_FILE).exists()
    assert area == sentinel, (
        f"half-erased root (kill_at={kill_at}): area={area} sentinel={sentinel}"
    )
    if kill_at < 2:  # the erasure never ran: the intention is given up, the folder is untouched
        assert area and (env.library / RESERVED_DIR / "tombstones").read_bytes() == b"journal"
        assert report.cancelled == 1
    else:  # it ran: it is gone, whole, and the intention is closed
        assert not area
