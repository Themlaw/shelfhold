"""M4 L04 (EF-EXP-001, EF-EXP-060): `Fsops.utime`, the time of a file set in place.

The inode, the hard links, the mode and the bytes stay; the access time is kept. Bounded like every
write: a link is not followed, a folder is not a file, an adoption root is never written."""

import os
from pathlib import Path

import pytest

from shelfhold.infrastructure.fsops import NotRegularFileError, ReadOnlyRootError
from tests.fsops_env import STRATEGIES, Env, make_env

pytestmark = pytest.mark.req("EF-EXP-001", "EF-EXP-060")
NS = 1_577_934_245_000_000_000


@pytest.fixture(params=STRATEGIES, ids=lambda s: s.value)
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Env:
    return make_env(tmp_path, strategy=request.param)


def test_utime_sets_the_time_and_nothing_else(env: Env) -> None:
    chapter = env.manga / "S" / "1.cbz"
    env.fsops.write_atomic(str(chapter), b"chapter", parents=True)
    kept = env.manga / "kept.cbz"
    os.link(chapter, kept)
    os.chmod(chapter, 0o640)
    before = chapter.stat()
    env.fsops.utime(str(chapter), mtime_ns=NS)
    after = chapter.stat()
    assert after.st_mtime_ns == NS
    assert (after.st_ino, after.st_nlink, after.st_mode, after.st_size) == (
        before.st_ino, before.st_nlink, before.st_mode, before.st_size,
    )  # fmt: skip
    assert after.st_atime_ns == before.st_atime_ns
    assert chapter.read_bytes() == b"chapter" and kept.stat().st_mtime_ns == NS


def test_utime_never_follows_a_link_and_never_touches_a_folder(env: Env) -> None:
    target = env.manga / "real.cbz"
    env.fsops.write_atomic(str(target), b"x", parents=True)
    link = env.manga / "link.cbz"
    link.symlink_to(target)
    folder = env.manga / "S"
    folder.mkdir()
    before = target.stat().st_mtime_ns
    for path in (link, folder, env.manga / "missing.cbz"):
        with pytest.raises((NotRegularFileError, OSError)):
            env.fsops.utime(str(path), mtime_ns=NS)
    assert target.stat().st_mtime_ns == before


def test_utime_is_refused_on_an_adoption_root(tmp_path: Path) -> None:
    env = make_env(tmp_path)
    chapter = env.upstream / "1.cbz"
    chapter.write_bytes(b"upstream")
    before = chapter.stat().st_mtime_ns
    with pytest.raises(ReadOnlyRootError):
        env.fsops.utime(str(chapter), mtime_ns=NS)
    assert chapter.stat().st_mtime_ns == before
