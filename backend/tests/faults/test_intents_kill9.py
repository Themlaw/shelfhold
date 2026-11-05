"""EF-EXP-041: a plan of 10 renames, a real process killed by SIGKILL after every step of the
journal protocol, then a restart: 0 duplicate, 0 loss, a manifest that matches the disk."""

import hashlib
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.application.intents import IntentJournal, OpenIntent, open_intent_paths
from shelfhold.infrastructure.fsops import Fsops
from tests.intents_env import make_intent_env

RENAMES = 10
STEPS_PER_RENAME = 4  # record (pending), applying, the rename itself, done
TOTAL_STEPS = RENAMES * STEPS_PER_RENAME

CHILD = textwrap.dedent(
    """
    import os, signal, sys
    from pathlib import Path
    import sqlalchemy as sa
    from shelfhold.export.application.intents import IntentJournal, RenameStep
    from shelfhold.export.domain.path_map import PathEntry, PathMap
    from shelfhold.export.domain.physical_roots import ExpectedRoot
    from shelfhold.infrastructure.db import create_db_engine
    from shelfhold.infrastructure.fsops import Fsops

    library, root_id, instance_id, database, kill_at = sys.argv[1:6]
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
        (journal, "record"), (journal, "mark_applying"), (fsops, "rename"), (journal, "mark_done"),
    ]:
        die_after(owner, name)

    def manifest(conn, intent):
        conn.execute(
            sa.text("UPDATE manifest SET path = :dst WHERE path = :src"),
            {"src": intent.src, "dst": intent.dst},
        )

    steps = [
        RenameStep(src=f"{library}/manga/S/{i}.cbz", dst=f"{library}/manga/S/{i} renamed.cbz")
        for i in range(10)
    ]
    journal.apply_rename_plan(steps, on_done=manifest)
    """
)


def content(i: int) -> bytes:
    return f"chapter {i}\n".encode() * 500


def manifest_rows(conn: Connection) -> set[str]:
    return {r.path for r in conn.execute(sa.text("SELECT path FROM manifest"))}


@pytest.mark.req("EF-EXP-041")
@pytest.mark.parametrize("kill_at", range(TOTAL_STEPS + 1))
def test_kill9_at_each_step_of_10_renames_no_duplicate_no_loss(
    tmp_path: Path, kill_at: int
) -> None:
    ienv = make_intent_env(tmp_path)
    env = ienv.fs
    series = env.manga / "S"
    sources = [series / f"{i}.cbz" for i in range(RENAMES)]
    targets = [series / f"{i} renamed.cbz" for i in range(RENAMES)]
    for i, source in enumerate(sources):
        env.fsops.write_atomic(str(source), content(i), parents=True)
    with ienv.engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE manifest (path TEXT PRIMARY KEY)"))
        conn.execute(sa.text("INSERT INTO manifest VALUES (:p)"), [{"p": str(s)} for s in sources])

    root = env.expected[0]
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CHILD, str(env.library), root.root_id, root.instance_id,
         str(tmp_path / "shelfhold.db"), str(kill_at)],
        check=False,
        capture_output=True,
    )  # fmt: skip
    if kill_at < TOTAL_STEPS:
        assert result.returncode == -signal.SIGKILL, result.stderr.decode()
    else:
        assert result.returncode == 0, result.stderr.decode()  # no kill point left: it just ends

    # The restart: boot the roots, then resolve every open intent before anything else.
    fsops = Fsops.open(env.path_map, env.expected)
    fsops.boot()
    journal = IntentJournal(ienv.engine, fsops, clock=ienv.clock)

    def manifest(conn: Connection, intent: OpenIntent) -> None:
        conn.execute(
            sa.text("UPDATE manifest SET path = :dst WHERE path = :src"),
            {"src": intent.src, "dst": intent.dst},
        )

    journal.resolve_open(on_done=manifest)

    with ienv.engine.connect() as conn:
        assert open_intent_paths(conn) == frozenset()
        rows = manifest_rows(conn)
    leftovers = sorted(p.name for p in series.iterdir())
    for i, (source, target) in enumerate(zip(sources, targets, strict=True)):
        where = [p for p in (source, target) if p.exists()]
        assert len(where) == 1, f"rename {i}: {len(where)} copies (kill_at={kill_at}): {leftovers}"
        assert hashlib.sha256(where[0].read_bytes()).digest() == hashlib.sha256(content(i)).digest()
        assert str(where[0]) in rows  # the manifest says what the disk says
    assert len(rows) == RENAMES
    assert len(leftovers) == RENAMES  # no stray temporary or duplicate file
    # Renames that never started stay untouched; the one that was cut is completed or replayed.
    started = min(RENAMES, kill_at // STEPS_PER_RENAME + 1)
    for i in range(started, RENAMES):
        assert sources[i].exists()
