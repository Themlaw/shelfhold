"""L25: `reconcile --rebaseline-inodes` (EF-EXP-043)."""

import os
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.rebaseline import Rebaseliner, RebaselineRefusedError
from tests.restore_env import RestoreEnv, restore_env
from tests.root_exits_env import tree


@pytest.fixture
def env() -> Iterator[RestoreEnv]:
    with restore_env(first_chapters=3) as e:
        yield e


def move_disks(files: list[Path]) -> None:
    """What `rsync -a` to a new disk does to the identities: same bytes, same mtime, new inode."""
    for path in files:
        stat = path.stat()
        copy = path.with_name(path.name + ".moving")
        shutil.copyfile(path, copy)
        os.utime(copy, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(copy, path)


def identities(env: RestoreEnv) -> dict[str, tuple[int | None, int | None]]:
    with env.engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT rel_path, dev, ino FROM replica WHERE location = 'destination'")
        ).all()
    return {r.rel_path: (r.dev, r.ino) for r in rows}


def run(env: RestoreEnv) -> object:
    return Rebaseliner(env.engine, env.fsops, clock=env.ex.clock).run()


def states(env: RestoreEnv) -> set[str]:
    with env.engine.connect() as connection:
        return set(connection.execute(sa.text("SELECT state FROM export_record")).scalars())


@pytest.mark.req("EF-EXP-043")
def test_rebaseline_inodes_after_disk_move_no_pause(env: RestoreEnv) -> None:
    files = env.managed()
    before = tree(env.library)
    move_disks(files)
    report = run(env)
    assert len(report.updated) == len(files)  # type: ignore[attr-defined]
    assert not report.skipped  # type: ignore[attr-defined]
    # the registry follows the disk: a second run finds nothing to change
    again = run(env)
    assert not again.updated  # type: ignore[attr-defined]
    assert again.unchanged == len(files)  # type: ignore[attr-defined]
    assert tree(env.library).keys() == before.keys()
    assert "paused" not in states(env) and "modified" not in states(env)


@pytest.mark.req("EF-EXP-043")
def test_rebaseline_refuses_when_file_hash_differs(env: RestoreEnv) -> None:
    files = env.managed()
    move_disks(files)
    stat = files[0].stat()
    files[0].write_bytes(b"edited by a third party" + files[0].read_bytes()[23:])
    os.utime(files[0], ns=(stat.st_atime_ns, stat.st_mtime_ns))
    registry = identities(env)
    with pytest.raises(RebaselineRefusedError) as refusal:
        run(env)
    assert refusal.value.reason == "hash_differs"
    assert identities(env) == registry  # nothing changed, not even for the files that match


@pytest.mark.req("EF-EXP-043")
def test_rebaseline_needs_the_mirror(env: RestoreEnv) -> None:
    move_disks(env.managed())
    shutil.rmtree(env.library / ".shelfhold" / "manifest")
    registry = identities(env)
    with pytest.raises(RebaselineRefusedError) as refusal:
        run(env)
    assert refusal.value.reason == "mirror_unconfirmed"
    assert identities(env) == registry
