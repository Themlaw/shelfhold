"""Regression tests from the review of PR #30 (PoCs turned into tests)."""

import os
import shutil
import subprocess
import sys
import textwrap
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from shelfhold.export.domain.physical_roots import RootState
from shelfhold.infrastructure.fsops import ConfinedRoot, FsopsError, Strategy, openat2_supported
from tests.fsops_env import STRATEGIES, Env, make_env


def within(seconds: float, call: Callable[[], Any], unblock: Callable[[], None]) -> Any:
    """Run `call` in a thread; a hang fails the test instead of freezing the suite."""
    outcome: dict[str, Any] = {}

    def go() -> None:
        try:
            outcome["value"] = call()
        except BaseException as error:
            outcome["error"] = error

    thread = threading.Thread(target=go, daemon=True)
    thread.start()
    thread.join(seconds)
    hung = thread.is_alive()
    if hung:
        unblock()
        thread.join(2)
    assert not hung, "the call hangs on a special file"
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


def release_fifo(path: Path) -> Callable[[], None]:
    def unblock() -> None:
        fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        os.close(fd)

    return unblock


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return make_env(tmp_path)


@pytest.mark.req("EF-EXP-059", "EF-EXP-029")
def test_sentinel_replaced_by_a_fifo_blocks_its_root_without_hanging_boot(env: Env) -> None:
    sentinel = env.library / ".shelfhold-mount-id"
    sentinel.unlink()
    os.mkfifo(sentinel)
    report = within(3, env.fsops.boot, release_fifo(sentinel))
    assert report.blocked == {str(env.library): RootState.MISMATCH}
    assert report.ok == [str(env.books)]  # the other root goes on


@pytest.mark.req("EF-EXP-059", "EF-EXP-029")
def test_sentinel_replaced_by_a_directory_blocks_its_root_and_boot_goes_on(env: Env) -> None:
    sentinel = env.library / ".shelfhold-mount-id"
    sentinel.unlink()
    sentinel.mkdir()
    report = env.fsops.boot()
    assert report.blocked == {str(env.library): RootState.MISMATCH}
    assert report.ok == [str(env.books)]
    with pytest.raises(OSError, match="blocked"):
        env.fsops.write_atomic(str(env.manga / "x.cbz"), b"x")


@pytest.mark.req("EF-EXP-059", "ENF-SEC-012")
@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.value)
def test_fifo_is_refused_by_reads_and_copies(tmp_path: Path, strategy: Strategy) -> None:
    env = make_env(tmp_path, strategy=strategy)
    fifo = env.manga / "evil.cbz"
    os.mkfifo(fifo)
    with pytest.raises(FsopsError):
        within(3, lambda: env.fsops.read_bytes(str(fifo)), release_fifo(fifo))
    with pytest.raises(FsopsError):
        within(
            3,
            lambda: env.fsops.copy_across(str(fifo), str(env.books / "evil.cbz")),
            release_fifo(fifo),
        )
    assert not (env.books / "evil.cbz").exists()
    with ConfinedRoot.open(env.library, strategy=strategy) as root, pytest.raises(FsopsError):
        within(3, lambda: root.open_file("manga/evil.cbz", os.O_RDONLY), release_fifo(fifo))


@pytest.mark.req("ENF-SEC-012")
def test_regular_files_stay_blocking_after_the_non_blocking_open(env: Env) -> None:
    target = env.manga / "ok.cbz"
    target.write_bytes(b"x")
    with ConfinedRoot.open(env.library) as root:
        fd = root.open_file("manga/ok.cbz", os.O_RDONLY)
        try:
            assert os.get_blocking(fd)
        finally:
            os.close(fd)


UNSHARE_SCRIPT = textwrap.dedent(
    """
    import os, subprocess, sys
    from pathlib import Path
    from shelfhold.infrastructure.fsops import ConfinedRoot, FsopsError, Strategy

    base, strategy = Path(sys.argv[1]), Strategy(sys.argv[2])
    root, other = base / "root", base / "other"
    (root / "X").mkdir(parents=True)
    other.mkdir()
    (other / "precious.cbz").write_bytes(b"data")
    subprocess.run(["mount", "--bind", str(other), str(root / "X")], check=True)
    before = sorted(os.listdir(other))
    with ConfinedRoot.open(root, strategy=strategy) as confined:
        calls = {
            "mkdir": lambda: confined.mkdir("X/d"),
            "mkdir-parents": lambda: confined.mkdir("X/d/e", parents=True),
            "unlink": lambda: confined.unlink("X/precious.cbz"),
            "create": lambda: confined.open_file("X/new", os.O_WRONLY | os.O_CREAT, 0o644),
            "rename": lambda: confined.rename("X/precious.cbz", "p", replace=False),
        }
        for name, call in calls.items():
            try:
                call()
            except FsopsError:
                continue
            raise SystemExit(f"{name} crossed the undeclared mount")
    assert sorted(os.listdir(other)) == before, os.listdir(other)
    assert (other / "precious.cbz").read_bytes() == b"data"
    print("refused")
    """
)


def unshare_mounts() -> str | None:
    """None when `unshare -Urm` plus a bind mount works here, otherwise the reason."""
    if shutil.which("unshare") is None or shutil.which("mount") is None:
        return "unshare or mount is not installed"
    probe = "mkdir -p /tmp/_a /tmp/_b && mount --bind /tmp/_a /tmp/_b"
    result = subprocess.run(  # noqa: S603
        ["unshare", "-Urm", "sh", "-c", probe],  # noqa: S607
        capture_output=True,
        check=False,
    )
    return None if result.returncode == 0 else "unprivileged mount namespaces are not allowed"


@pytest.mark.req("ENF-SEC-012", "EF-EXP-059")
@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.value)
def test_undeclared_submount_inside_a_root_is_never_crossed(
    tmp_path: Path, strategy: Strategy
) -> None:
    reason = unshare_mounts()
    if reason:
        pytest.skip(reason)
    result = subprocess.run(  # noqa: S603
        [  # noqa: S607
            "unshare",
            "-Urm",
            sys.executable,
            "-c",
            UNSHARE_SCRIPT,
            str(tmp_path),
            strategy.value,
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "refused"


@pytest.mark.req("ENF-SEC-012")
def test_fallback_compares_mount_ids_and_falls_back_to_st_dev_when_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from shelfhold.infrastructure.fsops import confined

    (tmp_path / "a" / "b").mkdir(parents=True)
    with ConfinedRoot.open(tmp_path, strategy=Strategy.FALLBACK) as root:
        monkeypatch.setattr(confined, "mount_id_of", lambda _fd: None)
        # unreadable fdinfo: st_dev is compared instead (same device here, so it passes)
        root.mkdir("a/b/c")
        assert (tmp_path / "a" / "b" / "c").is_dir()


@pytest.mark.req("EF-EXP-006", "ENF-SEC-012")
@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.value)
def test_rename_never_overwrites_unless_asked(tmp_path: Path, strategy: Strategy) -> None:
    env = make_env(tmp_path, strategy=strategy)
    one, two = env.manga / "f1", env.manga / "f2"
    one.write_bytes(b"one")
    two.write_bytes(b"two")
    with pytest.raises(FileExistsError):
        env.fsops.rename(str(one), str(two))
    assert (one.read_bytes(), two.read_bytes()) == (b"one", b"two")

    (env.manga / "s1" / "sub").mkdir(parents=True)
    (env.manga / "s2").mkdir()
    with pytest.raises(FileExistsError):  # POSIX would silently replace an empty directory
        env.fsops.rename(str(env.manga / "s1"), str(env.manga / "s2"))
    assert (env.manga / "s1" / "sub").is_dir()

    env.fsops.rename(str(one), str(two), replace=True)
    assert two.read_bytes() == b"one"
    assert not one.exists()
    env.fsops.rename(str(two), str(env.manga / "f3"))
    assert (env.manga / "f3").read_bytes() == b"one"


@pytest.mark.req("EF-EXP-006")
def test_atomic_write_still_replaces_an_existing_file(env: Env) -> None:
    target = env.manga / "r.cbz"
    env.fsops.write_atomic(str(target), b"v1")
    env.fsops.write_atomic(str(target), b"v2")
    assert target.read_bytes() == b"v2"


@pytest.mark.req("ENF-SEC-012")
def test_openat2_probe_uses_the_root_descriptor_not_the_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[object] = []
    real_open = os.open

    def spy(path: Any, *args: Any, **kwargs: Any) -> int:
        opened.append(path)
        return real_open(path, *args, **kwargs)

    fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        monkeypatch.setattr(os, "open", spy)
        assert openat2_supported(fd)
    finally:
        os.close(fd)
    assert "." not in opened


@pytest.mark.req("EF-EXP-006")
def test_failed_recycle_leaves_no_empty_folder_behind(env: Env) -> None:
    with pytest.raises(FileNotFoundError):
        env.fsops.recycle(str(env.manga / "X" / "missing.cbz"))
    recycle = env.library / ".shelfhold" / "recycle"
    assert not recycle.exists() or list(recycle.iterdir()) == []
