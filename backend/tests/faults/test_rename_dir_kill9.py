"""kill -9 at each stage of the move of a series folder (M3 L29, EF-EXP-041, EF-EXP-030): the
folder is at the source or at the target, never both, never neither, and once the boot has
settled the intent the manifest says what the disk says."""

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

FILES = 3
STAGES = 4  # record, mark_applying, rename, mark_done

CHILD = textwrap.dedent(
    """
    import os, signal, sys
    from pathlib import Path
    import sqlalchemy as sa
    from shelfhold.export.application.intents import IntentJournal, MoveDirStep
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
            sa.text("UPDATE manifest SET path = :dst || substr(path, length(:src) + 1)"
                    " WHERE path LIKE :src || '/%'"),
            {"src": intent.src, "dst": intent.dst},
        )

    journal.apply_move_dir(
        MoveDirStep(f"{library}/manga/Old", f"{library}/manga/New"),
        payload={"scope": "series_dir"}, on_done=manifest,
    )
    """
)


def content(i: int) -> bytes:
    return f"chapter {i}\n".encode() * 500


def _manifest(conn: Connection, intent: OpenIntent) -> None:
    conn.execute(
        sa.text(
            "UPDATE manifest SET path = :dst || substr(path, length(:src) + 1)"
            " WHERE path LIKE :src || '/%'"
        ),
        {"src": intent.src, "dst": intent.dst},
    )


@pytest.mark.req("EF-EXP-041", "EF-EXP-030")
@pytest.mark.parametrize("kill_at", range(STAGES + 1))
def test_kill9_at_each_stage_of_a_series_folder_move(tmp_path: Path, kill_at: int) -> None:
    ienv = make_intent_env(tmp_path)
    env = ienv.fs
    old, new = env.manga / "Old", env.manga / "New"
    for i in range(FILES):
        env.fsops.write_atomic(str(old / f"{i}.cbz"), content(i), parents=True)
    inode = old.stat().st_ino
    with ienv.engine.begin() as conn:
        conn.execute(sa.text("CREATE TABLE manifest (path TEXT PRIMARY KEY)"))
        conn.execute(
            sa.text("INSERT INTO manifest VALUES (:p)"),
            [{"p": str(old / f"{i}.cbz")} for i in range(FILES)],
        )
    root = env.expected[0]
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CHILD, str(env.library), root.root_id, root.instance_id,
         str(tmp_path / "shelfhold.db"), str(kill_at)],
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
    journal.resolve_open(on_done=_manifest)

    with ienv.engine.connect() as conn:
        assert open_intent_paths(conn) == frozenset()
        rows = {r.path for r in conn.execute(sa.text("SELECT path FROM manifest"))}
    where = [d for d in (old, new) if d.exists()]
    assert len(where) == 1, f"the folder is in {len(where)} places (kill_at={kill_at})"
    assert where[0] == new  # a cut-short intent is replayed forward, never undone
    assert where[0].stat().st_ino == inode  # the same folder, moved: not copied, not recreated
    names = sorted(p.name for p in where[0].iterdir())
    assert names == [f"{i}.cbz" for i in range(FILES)]
    for i in range(FILES):
        digest = hashlib.sha256((where[0] / f"{i}.cbz").read_bytes()).digest()
        assert digest == hashlib.sha256(content(i)).digest()
    assert rows == {str(where[0] / f"{i}.cbz") for i in range(FILES)}  # the manifest agrees
