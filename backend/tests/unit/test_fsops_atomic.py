"""EF-EXP-006, EF-EXP-059, EF-EXP-060: atomic writes, permissions, staging, cross-root copies."""

import errno
import hashlib
import io
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from shelfhold.infrastructure.fsops import (
    CrossRootError,
    FileIdentity,
    PathEscapeError,
    ReadOnlyRootError,
    ReservedPathError,
    Strategy,
    identity_of,
    same_file,
)
from tests.fsops_env import STRATEGIES, Env, make_env


@pytest.fixture(params=STRATEGIES, ids=lambda s: s.value)
def env(request: pytest.FixtureRequest, tmp_path: Path) -> Env:
    return make_env(tmp_path, strategy=request.param)


def dev_of_fd(fd: int) -> int:
    return os.fstat(fd).st_dev


@pytest.mark.req("EF-EXP-006")
def test_atomic_write_fsync_then_rename(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[tuple[str, str]] = []
    real_fsync, real_rename = os.fsync, os.rename

    def fsync(fd: int) -> None:
        kind = "dir" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file"
        events.append(("fsync", kind))
        real_fsync(fd)

    def rename(src: str, dst: str, **kwargs: object) -> None:
        events.append(("rename", dst))
        real_rename(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "replace", rename)
    target = env.manga / "X" / "1.cbz"
    env.fsops.mkdir(str(target.parent))
    events.clear()
    env.fsops.write_atomic(str(target), b"payload")

    assert target.read_bytes() == b"payload"
    kinds = [e for e in events if e[0] in {"fsync", "rename"}]
    assert kinds[0] == ("fsync", "file"), events
    assert kinds[1] == ("rename", "1.cbz"), events
    assert kinds[-1] == ("fsync", "dir"), events
    assert kinds.count(("rename", "1.cbz")) == 1
    # No staging leftover, and the staging area is the root's own.
    assert list((env.library / ".shelfhold" / "staging").iterdir()) == []


@pytest.mark.req("EF-EXP-006")
def test_failed_write_leaves_neither_a_partial_target_nor_a_staging_file(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = env.manga / "old.cbz"
    env.fsops.write_atomic(str(target), b"old")

    class Boom(Exception): ...

    def fail(_fd: int) -> None:
        raise Boom

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(Boom):
        env.fsops.write_atomic(str(target), b"new")
    monkeypatch.undo()
    assert target.read_bytes() == b"old"
    assert list((env.library / ".shelfhold" / "staging").iterdir()) == []


@pytest.mark.req("EF-EXP-006")
def test_write_accepts_a_stream_and_returns_the_identity_of_what_was_written(env: Env) -> None:
    target = env.manga / "s.cbz"
    identity = env.fsops.write_atomic(str(target), io.BytesIO(b"abc" * 1000))
    assert identity.size == 3000
    assert same_file(identity, identity_of(target.stat()))


@pytest.mark.req("EF-EXP-006")
def test_write_replaces_an_existing_file_atomically_and_bumps_its_mtime(env: Env) -> None:
    target = env.manga / "r.cbz"
    env.fsops.write_atomic(str(target), b"v1")
    os.utime(target, ns=(1, 1))
    env.fsops.write_atomic(str(target), b"v2", mtime_ns=1_700_000_000_000_000_000)
    assert target.read_bytes() == b"v2"
    assert target.stat().st_mtime_ns == 1_700_000_000_000_000_000


@pytest.mark.req("EF-EXP-006")
def test_existing_file_refused_when_replace_is_not_requested(env: Env) -> None:
    target = env.manga / "once.cbz"
    env.fsops.write_atomic(str(target), b"v1")
    with pytest.raises(FileExistsError):
        env.fsops.write_atomic(str(target), b"v2", replace=False)
    assert target.read_bytes() == b"v1"


@pytest.mark.req("EF-EXP-006")
def test_no_chown_umask_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_a: object, **_k: object) -> None:
        raise AssertionError("fsops must never change ownership")

    for name in ("chown", "fchown", "lchown"):
        monkeypatch.setattr(os, name, forbidden)

    previous = os.umask(0o022)
    try:
        # SHELFHOLD_UMASK given: applied to files and to the directories created on the way.
        env = make_env(tmp_path / "explicit", umask=0o077)
        env.fsops.write_atomic(str(env.manga / "new" / "a.cbz"), b"x", parents=True)
        assert stat.S_IMODE((env.manga / "new" / "a.cbz").stat().st_mode) == 0o600
        assert stat.S_IMODE((env.manga / "new").stat().st_mode) == 0o700

        # No setting: the process umask rules, exactly as for any other file.
        os.umask(0o027)
        env = make_env(tmp_path / "inherited", umask=None)
        env.fsops.write_atomic(str(env.manga / "new" / "b.cbz"), b"x", parents=True)
        assert stat.S_IMODE((env.manga / "new" / "b.cbz").stat().st_mode) == 0o640
        assert stat.S_IMODE((env.manga / "new").stat().st_mode) == 0o750
    finally:
        os.umask(previous)
    owner = (env.manga / "new" / "b.cbz").stat()
    assert (owner.st_uid, owner.st_gid) == (os.getuid(), os.getgid())


@pytest.mark.req("EF-EXP-006")
def test_the_process_umask_is_left_as_found(env: Env) -> None:
    previous = os.umask(0o022)
    try:
        env.fsops.write_atomic(str(env.manga / "u.cbz"), b"x")
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(previous)


@pytest.mark.req("EF-EXP-003", "EF-EXP-006")
def test_staging_on_same_mount_no_exdev(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[int, int]] = []
    real_rename = os.rename

    def rename(
        src: str, dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        assert src_dir_fd is not None and dst_dir_fd is not None, "renames are fd-relative"
        seen.append((dev_of_fd(src_dir_fd), dev_of_fd(dst_dir_fd)))
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "replace", rename)
    env.fsops.write_atomic(str(env.manga / "e.cbz"), b"x")
    assert seen
    assert all(src == dst for src, dst in seen)
    staging = env.library / ".shelfhold" / "staging"
    assert staging.is_dir()
    assert staging.stat().st_dev == (env.manga).stat().st_dev


@pytest.mark.req("EF-EXP-059")
def test_library_subdir_on_other_device_uses_local_staging_and_sentinel(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """/library/books is its own root: its staging lives in it, and a rename never crosses."""
    real_rename = os.rename
    parents: list[str] = []

    def rename(
        src: str, dst: str, *, src_dir_fd: int | None = None, dst_dir_fd: int | None = None
    ) -> None:
        src_dir = os.readlink(f"/proc/self/fd/{src_dir_fd}")
        dst_dir = os.readlink(f"/proc/self/fd/{dst_dir_fd}")
        parents.append(src_dir)
        # What the kernel would do if books were another mount.
        fake_dev = lambda p: 2 if p.startswith(str(env.books)) else 1  # noqa: E731
        if fake_dev(src_dir) != fake_dev(dst_dir):
            raise OSError(errno.EXDEV, "cross-device link")
        real_rename(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "replace", rename)
    env.fsops.write_atomic(str(env.books / "Y" / "1.epub"), b"b", parents=True)
    env.fsops.write_atomic(str(env.manga / "X" / "1.cbz"), b"m", parents=True)

    assert (env.books / "Y" / "1.epub").read_bytes() == b"b"
    assert any(p.startswith(str(env.books / ".shelfhold" / "staging")) for p in parents)
    assert not (env.library / ".shelfhold" / "staging" / "Y").exists()
    # Each root keeps its own sentinel.
    assert env.sentinel_of(env.books)["root_id"] != env.sentinel_of(env.library)["root_id"]


@pytest.mark.req("EF-EXP-059")
def test_cross_root_write_never_renames(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    source = env.manga / "X" / "1.cbz"
    env.fsops.write_atomic(str(source), b"content", parents=True)
    destination = env.books / "X" / "1.cbz"

    with pytest.raises(CrossRootError):
        env.fsops.rename(str(source), str(destination))
    assert source.exists()
    assert not destination.exists()

    cross_renames: list[tuple[str, str]] = []
    real_rename = os.rename

    def rename(src: str, dst: str, **kwargs: int | None) -> None:
        cross_renames.append((src, dst))
        real_rename(src, dst, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "rename", rename)
    monkeypatch.setattr(os, "replace", rename)
    digest = env.fsops.copy_across(str(source), str(destination), parents=True)
    # Copy, verification, then only the destination root's own staging rename.
    assert digest == hashlib.sha256(b"content").hexdigest()
    assert destination.read_bytes() == b"content"
    assert source.read_bytes() == b"content"
    assert all(dst == "1.cbz" for _src, dst in cross_renames)
    assert source.stat().st_ino != destination.stat().st_ino


@pytest.mark.req("EF-EXP-059")
def test_cross_root_copy_that_fails_verification_leaves_nothing(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = env.manga / "c.cbz"
    env.fsops.write_atomic(str(source), b"content")
    monkeypatch.setattr(
        "shelfhold.infrastructure.fsops.writer.verify_copy",
        lambda *_a, **_k: False,
        raising=True,
    )
    with pytest.raises(OSError, match="verification"):
        env.fsops.copy_across(str(source), str(env.books / "c.cbz"))
    assert not (env.books / "c.cbz").exists()
    assert list((env.books / ".shelfhold" / "staging").iterdir()) == []


@pytest.mark.req("EF-EXP-006")
def test_file_identity_survives_dev_change(env: Env) -> None:
    """`expected_file_id` is (ino, size, mtime); `dev` is only meaningful within one boot."""
    target = env.manga / "i.cbz"
    env.fsops.write_atomic(str(target), b"identity")
    observed = target.stat()
    identity = identity_of(observed)
    assert FileIdentity.from_json(identity.to_json()) == identity
    assert "dev" not in identity.to_json()

    moved = SimpleNamespace(
        st_ino=observed.st_ino,
        st_dev=observed.st_dev + 41,  # the disk came back with another device number
        st_size=observed.st_size,
        st_mtime_ns=observed.st_mtime_ns,
    )
    assert same_file(identity, identity_of(moved))
    assert not same_file(identity, identity_of(SimpleNamespace(**{**vars(moved), "st_ino": 1})))

    target.write_bytes(b"identity!")
    assert not same_file(identity, identity_of(target.stat()))


@pytest.mark.req("EF-EXP-060")
def test_adopt_ro_root_never_written(env: Env) -> None:
    series = env.upstream / "Series"
    series.mkdir()
    chapter = series / "1.cbz"
    chapter.write_bytes(b"upstream")
    before = sorted(p.name for p in env.upstream.rglob("*"))

    for call in (
        lambda: env.fsops.write_atomic(str(series / "new.cbz"), b"x"),
        lambda: env.fsops.write_atomic(str(chapter), b"x"),
        lambda: env.fsops.mkdir(str(series / "sub")),
        lambda: env.fsops.rename(str(chapter), str(series / "2.cbz")),
        lambda: env.fsops.rename(str(env.manga / "a"), str(series / "a")),
        lambda: env.fsops.unlink(str(chapter)),
        lambda: env.fsops.recycle(str(chapter)),
        lambda: env.fsops.rmdir(str(series)),
        lambda: env.fsops.copy_across(str(env.manga / "a"), str(series / "a")),
    ):
        with pytest.raises(ReadOnlyRootError):
            call()
    assert sorted(p.name for p in env.upstream.rglob("*")) == before
    assert chapter.read_bytes() == b"upstream"
    assert not (env.upstream / ".shelfhold").exists()
    # ... but it is readable, and is the source of a repatriation copy to a rw root.
    assert env.fsops.read_bytes(str(chapter)) == b"upstream"
    digest = env.fsops.copy_across(str(chapter), str(env.manga / "Series" / "1.cbz"), parents=True)
    assert digest == hashlib.sha256(b"upstream").hexdigest()
    assert chapter.read_bytes() == b"upstream"


@pytest.mark.req("EF-EXP-060", "ENF-SEC-012")
@pytest.mark.parametrize(
    "rel",
    [
        ".shelfhold-mount-id",
        ".shelfhold",
        ".shelfhold/staging/x",
        ".shelfhold/recycle/x",
        ".shelfhold/lease",
        "./.shelfhold/manifest/x",
        "manga/../.shelfhold/tombstones",
        ".shelfhold//lease",
    ],
)
def test_reserved_shelfhold_paths_refused_in_fsops(env: Env, rel: str) -> None:
    reserved = str(env.library / rel)
    before = env.sentinel_of(env.library)
    for call in (
        lambda: env.fsops.write_atomic(reserved, b"x", parents=True),
        lambda: env.fsops.mkdir(reserved),
        lambda: env.fsops.unlink(reserved),
        lambda: env.fsops.rmdir(reserved),
        lambda: env.fsops.recycle(reserved),
        lambda: env.fsops.rename(reserved, str(env.manga / "stolen")),
        lambda: env.fsops.rename(str(env.manga / "a"), reserved),
        lambda: env.fsops.copy_across(str(env.manga / "a"), reserved),
        lambda: env.fsops.read_bytes(reserved),
    ):
        with pytest.raises((ReservedPathError, PathEscapeError)):
            call()
    assert env.sentinel_of(env.library) == before
    assert not (env.library / "stolen").exists()


@pytest.mark.req("EF-EXP-006", "EF-EXP-060")
def test_recycle_moves_inside_the_same_root_never_across(env: Env) -> None:
    target = env.books / "Y" / "1.epub"
    env.fsops.write_atomic(str(target), b"b", parents=True)
    kept = env.fsops.recycle(str(target))
    assert not target.exists()
    assert Path(kept).is_relative_to(env.books / ".shelfhold" / "recycle")
    assert Path(kept).read_bytes() == b"b"


@pytest.mark.req("EF-EXP-006")
def test_strategy_auto_and_forced_agree(tmp_path: Path) -> None:
    results = []
    for index, strategy in enumerate([Strategy.AUTO, *STRATEGIES]):
        env = make_env(tmp_path / str(index), strategy=strategy)
        env.fsops.write_atomic(str(env.manga / "X" / "1.cbz"), b"same", parents=True)
        results.append((env.manga / "X" / "1.cbz").read_bytes())
    assert results == [b"same"] * 3
