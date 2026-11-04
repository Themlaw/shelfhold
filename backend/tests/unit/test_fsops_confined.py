"""ENF-SEC-012: every path is resolved beneath an open root descriptor, symlinks refused.

Each behaviour runs under both strategies: `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS |
RESOLVE_NO_MAGICLINKS)` and the component-by-component fallback (`O_NOFOLLOW` + `dir_fd`).
"""

import ast
import contextlib
import errno
import os
import stat
from pathlib import Path

import pytest

from shelfhold.infrastructure import fsops
from shelfhold.infrastructure.fsops import (
    ConfinedRoot,
    FsopsError,
    PathEscapeError,
    Strategy,
    openat2_supported,
)

STRATEGIES = [Strategy.OPENAT2, Strategy.FALLBACK]
pytestmark = pytest.mark.req("ENF-SEC-012")


@pytest.fixture(params=STRATEGIES, ids=lambda s: s.value)
def root(request: pytest.FixtureRequest, tmp_path: Path) -> ConfinedRoot:
    if request.param is Strategy.OPENAT2 and not openat2_supported():
        pytest.skip("openat2 not available on this kernel")
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret").write_text("s", encoding="utf-8")
    base = tmp_path / "root"
    base.mkdir()
    return ConfinedRoot.open(base, strategy=request.param)


def outside(root: ConfinedRoot) -> Path:
    return Path(root.path).parent / "outside"


def test_openat2_is_available_where_the_suite_runs() -> None:
    # The container image ships a kernel >= 5.6; the fallback exists for the others.
    assert openat2_supported()
    assert ConfinedRoot.open(Path("."), strategy=Strategy.AUTO).strategy is Strategy.OPENAT2


def test_fallback_is_chosen_when_openat2_is_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fsops, "openat2_supported", lambda: False)
    monkeypatch.setattr("shelfhold.infrastructure.fsops.confined.openat2_supported", lambda: False)
    assert ConfinedRoot.open(tmp_path, strategy=Strategy.AUTO).strategy is Strategy.FALLBACK


def test_forcing_openat2_without_kernel_support_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shelfhold.infrastructure.fsops.confined.openat2_supported", lambda: False)
    with pytest.raises(FsopsError):
        ConfinedRoot.open(tmp_path, strategy=Strategy.OPENAT2)


def test_regular_operations_stay_beneath_the_root(root: ConfinedRoot) -> None:
    root.mkdir("a/b", parents=True)
    fd = root.open_file("a/b/f", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    os.write(fd, b"x")
    os.close(fd)
    assert (Path(root.path) / "a/b/f").read_bytes() == b"x"
    assert sorted(root.listdir("a")) == ["b"]
    assert stat.S_ISREG(root.stat("a/b/f").st_mode)
    root.rename("a/b/f", "a/g")
    root.unlink("a/g")
    root.rmdir("a/b")
    assert root.listdir("a") == []


@pytest.mark.parametrize("target_kind", ["absolute", "relative", "dot", "self", "loop"])
def test_symlink_as_a_component_is_refused(root: ConfinedRoot, target_kind: str) -> None:
    base = Path(root.path)
    (base / "d").mkdir()
    targets = {
        "absolute": str(outside(root)),
        "relative": "../outside",
        "dot": ".",
        "self": str(base / "d"),
        "loop": "link",
    }
    os.symlink(targets[target_kind], base / "link")
    for call in (
        lambda: root.open_file("link/secret", os.O_RDONLY),
        lambda: root.open_file("link/new", os.O_WRONLY | os.O_CREAT, 0o644),
        lambda: root.mkdir("link/sub"),
        lambda: root.listdir("link"),
        lambda: root.stat("link/secret"),
        lambda: root.rename("d", "link/d"),
        lambda: root.unlink("link/secret"),
    ):
        with pytest.raises((PathEscapeError, OSError)):
            call()
    assert sorted(p.name for p in outside(root).iterdir()) == ["secret"]
    assert (outside(root) / "secret").read_text(encoding="utf-8") == "s"


def test_symlink_as_the_final_component_is_never_followed(root: ConfinedRoot) -> None:
    base = Path(root.path)
    os.symlink(outside(root) / "secret", base / "leaf")
    with pytest.raises((PathEscapeError, OSError)):
        root.open_file("leaf", os.O_RDONLY)
    with pytest.raises((PathEscapeError, OSError)):
        root.open_file("leaf", os.O_WRONLY | os.O_TRUNC)
    assert (outside(root) / "secret").read_text(encoding="utf-8") == "s"


def test_unlink_removes_the_link_itself_never_its_target(root: ConfinedRoot) -> None:
    base = Path(root.path)
    os.symlink(outside(root) / "secret", base / "leaf")
    with contextlib.suppress(PathEscapeError, OSError):
        root.unlink("leaf")
    assert (outside(root) / "secret").exists()


def test_symlink_in_a_managed_tree_is_reported_not_traversed(root: ConfinedRoot) -> None:
    base = Path(root.path)
    os.symlink("nowhere", base / "dangling")
    assert root.lstat_kind("dangling") == "symlink"
    assert root.lstat_kind("missing") is None


@pytest.mark.parametrize(
    "rel",
    ["/etc/passwd", "../outside/secret", "a/../../outside/secret", "..", "a/..", "x\0y", ""],
)
def test_absolute_dotdot_nul_and_empty_paths_are_refused(root: ConfinedRoot, rel: str) -> None:
    (Path(root.path) / "a").mkdir()
    with pytest.raises(PathEscapeError):
        root.open_file(rel, os.O_RDONLY)


def test_the_root_itself_may_be_reached_through_a_symlink_but_nothing_below(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "mount").symlink_to(real)  # e.g. a bind mount path configured by the admin
    with ConfinedRoot.open(tmp_path / "mount") as opened:
        opened.mkdir("a")
        assert (real / "a").is_dir()


def test_magic_links_are_refused(root: ConfinedRoot) -> None:
    fd = os.open(outside(root), os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.symlink(f"/proc/self/fd/{fd}", Path(root.path) / "proc")
        with pytest.raises((PathEscapeError, OSError)):
            root.open_file("proc/secret", os.O_RDONLY)
    finally:
        os.close(fd)


def test_symlink_planted_between_two_components_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic TOCTOU for the fallback: the directory is swapped right before it is opened."""
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "secret").write_text("s", encoding="utf-8")
    base = tmp_path / "root"
    (base / "a" / "b").mkdir(parents=True)
    root = ConfinedRoot.open(base, strategy=Strategy.FALLBACK)
    real_open = os.open
    swapped: list[str] = []

    def racing_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if path == "b" and not swapped:
            swapped.append(path)
            (base / "a" / "b").rmdir()
            os.symlink(tmp_path / "outside", base / "a" / "b")
        return real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", racing_open)
    with pytest.raises((PathEscapeError, OSError)) as caught:
        root.open_file("a/b/new", os.O_WRONLY | os.O_CREAT, 0o644)
    assert swapped, "the hook never fired: the fallback no longer walks component by component"
    if isinstance(caught.value, OSError):
        assert caught.value.errno in (errno.ELOOP, errno.ENOTDIR)
    assert not (tmp_path / "outside" / "new").exists()


def test_escape_errors_are_distinguishable_from_missing_files(root: ConfinedRoot) -> None:
    os.symlink(outside(root), Path(root.path) / "link")
    with pytest.raises(PathEscapeError):
        root.stat("link/secret")
    with pytest.raises(FileNotFoundError):
        root.stat("nothing/here")


def test_closing_twice_and_using_after_close_are_safe(tmp_path: Path) -> None:
    opened = ConfinedRoot.open(tmp_path)
    opened.close()
    opened.close()
    with pytest.raises(FsopsError):
        opened.listdir(".")


def test_fsops_never_resolves_a_path_before_opening_it() -> None:
    """No `realpath` / `resolve` / `abspath` then `open`: the check and the use are one call."""
    package = Path(fsops.__file__).parent
    offenders: list[str] = []
    for source in package.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Attribute) and node.attr in {
                "realpath",
                "resolve",
                "abspath",
                "readlink",
                "samefile",
            }:
                offenders.append(f"{source.name}:{node.lineno}:{node.attr}")
    assert offenders == []


def test_fsops_never_chowns() -> None:
    package = Path(fsops.__file__).parent
    for source in package.glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
            used = node.attr if isinstance(node, ast.Attribute) else getattr(node, "id", "")
            assert "chown" not in used, f"{source.name}:{node.lineno}"
