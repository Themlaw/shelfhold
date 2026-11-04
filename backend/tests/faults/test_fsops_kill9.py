"""EF-EXP-006: kill -9 in the middle of a write leaves no partial file visible, and the orphan
staging file is purged at the next boot."""

import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from shelfhold.infrastructure.fsops import Fsops
from tests.fsops_env import make_env

CHILD = textwrap.dedent(
    """
    import os, signal, stat, sys
    from pathlib import Path
    from shelfhold.export.domain.path_map import PathEntry, PathMap
    from shelfhold.export.domain.physical_roots import ExpectedRoot
    from shelfhold.infrastructure.fsops import Fsops

    library, root_id, instance_id, step = sys.argv[1:5]
    path_map = PathMap((PathEntry(f"{library}/manga", "/manga"),))
    fsops = Fsops.open(path_map, [ExpectedRoot(root_id, library, instance_id, 3)])
    fsops.boot()

    def die(*_a, **_k):
        os.kill(os.getpid(), signal.SIGKILL)

    real_rename = os.rename
    real_fsync = os.fsync
    def die_on_file(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            die()
        real_fsync(fd)

    if step == "before_fsync":
        os.fsync = die_on_file
    elif step == "before_rename":
        os.rename = die
        os.replace = die
    elif step == "after_rename":
        def after(*a, **k):
            real_rename(*a, **k)
            die()
        os.rename = after
        os.replace = after
    fsops.write_atomic(f"{library}/manga/X/1.cbz", b"A" * 200_000, parents=True)
    """
)


@pytest.mark.req("EF-EXP-006")
@pytest.mark.parametrize("step", ["before_fsync", "before_rename", "after_rename"])
def test_kill9_during_write_leaves_no_partial_file(tmp_path: Path, step: str) -> None:
    env = make_env(tmp_path)
    target = env.manga / "X" / "1.cbz"
    # The previous version must survive a crash at any point before the rename.
    env.fsops.write_atomic(str(target), b"old", parents=True)

    root = env.expected[0]
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", CHILD, str(env.library), root.root_id, root.instance_id, step],
        check=False,
        capture_output=True,
    )
    assert result.returncode == -signal.SIGKILL, result.stderr.decode()

    content = target.read_bytes()
    if step == "after_rename":
        assert content == b"A" * 200_000
    else:
        assert content == b"old", "a partial or new file became visible before the rename"
    visible = sorted(p.name for p in target.parent.iterdir())
    assert visible == ["1.cbz"], visible  # nothing but the final name where Kavita looks

    staging = env.library / ".shelfhold" / "staging"
    orphans = list(staging.iterdir())
    assert len(orphans) == (0 if step == "after_rename" else 1)

    report = Fsops.open(env.path_map, env.expected).boot()
    assert list(staging.iterdir()) == []
    assert report.purged_staging == len(orphans)
    assert os.path.exists(target)
