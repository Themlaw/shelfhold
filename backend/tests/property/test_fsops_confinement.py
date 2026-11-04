"""ENF-SEC-012: no operation of fsops ever touches anything outside its root.

The properties plant real symbolic links in a temporary directory, before and between the
operations, then check that the directory standing for "the rest of the machine" is unchanged.
"""

import contextlib
import hashlib
import os
import threading
from pathlib import Path

import pytest
from hypothesis import HealthCheck, example, given, settings
from hypothesis import strategies as st

from shelfhold.infrastructure.fsops import ConfinedRoot, FsopsError, Strategy, openat2_supported

NAMES = ["a", "b", "c", "link", "x.cbz"]
rel_paths = st.lists(st.sampled_from(NAMES), min_size=1, max_size=4).map("/".join)
link_targets = st.sampled_from(
    ["OUT", "OUT/d", "OUT/secret", "../out", "../../out", "..", ".", "link", "a", "/", "PROC"]
)
operations = st.one_of(
    st.tuples(st.just("plant"), rel_paths, link_targets),
    st.tuples(st.just("write"), rel_paths, st.binary(max_size=8)),
    st.tuples(st.just("truncate"), rel_paths, st.just(b"")),
    st.tuples(st.just("mkdir"), rel_paths, st.booleans()),
    st.tuples(st.just("unlink"), rel_paths, st.just(None)),
    st.tuples(st.just("rmdir"), rel_paths, st.just(None)),
    st.tuples(st.just("rename"), rel_paths, rel_paths),
    st.tuples(st.just("read"), rel_paths, st.just(None)),
    st.tuples(st.just("listdir"), rel_paths, st.just(None)),
    st.tuples(st.just("stat"), rel_paths, st.just(None)),
)


def snapshot(base: Path) -> dict[str, str]:
    seen: dict[str, str] = {}
    for current, dirs, files in os.walk(base, followlinks=False):
        for name in dirs + files:
            path = Path(current) / name
            rel = str(path.relative_to(base))
            if path.is_symlink():
                seen[rel] = f"link:{os.readlink(path)}"
            elif path.is_dir():
                seen[rel] = "dir"
            else:
                seen[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return seen


def build_world(parent: Path) -> tuple[Path, Path]:
    base = parent / "world"
    out, root = base / "out", base / "root"
    (out / "d").mkdir(parents=True)
    (out / "secret").write_bytes(b"secret")
    for name in NAMES:  # decoys: `../out/a` etc. exist, so a followed link would "work"
        if name == "x.cbz":
            (out / name).write_bytes(b"d")
        else:
            (out / name).mkdir(exist_ok=True)
    root.mkdir()
    return root, out


def apply(root_dir: Path, root: ConfinedRoot, op: tuple[str, object, object], out: Path) -> None:
    kind, rel, extra = op
    assert isinstance(rel, str)
    try:
        if kind == "plant":  # the attacker, with plain os calls, inside the managed tree
            target = (
                str(extra).replace("OUT", str(out)).replace("PROC", f"/proc/{os.getpid()}/root")
            )
            parent = str(Path(rel).parent)
            # The test's own plain os calls must not follow an earlier planted link either.
            walked = root_dir
            for part in Path(parent).parts if parent != "." else ():
                walked = walked / part
                if walked.is_symlink() or (walked.exists() and not walked.is_dir()):
                    return
            if parent != ".":
                root.mkdir(parent, parents=True, exist_ok=True)
            link = root_dir / rel
            if link.is_symlink() or link.is_file():
                link.unlink()
            if not link.exists():
                os.symlink(target, link)
        elif kind == "write":
            assert isinstance(extra, bytes)
            if str(Path(rel).parent) != ".":
                # best effort: a refusal here must not stop the write from being attempted
                with contextlib.suppress(FsopsError, OSError):
                    root.mkdir(str(Path(rel).parent), parents=True, exist_ok=True)
            fd = root.open_file(rel, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
            os.write(fd, extra)
            os.close(fd)
        elif kind == "truncate":
            os.close(root.open_file(rel, os.O_WRONLY | os.O_TRUNC))
        elif kind == "mkdir":
            root.mkdir(rel, parents=bool(extra), exist_ok=True)
        elif kind == "unlink":
            root.unlink(rel)
        elif kind == "rmdir":
            root.rmdir(rel)
        elif kind == "rename":
            assert isinstance(extra, str)
            root.rename(rel, extra, replace=False)
        elif kind == "read":
            os.close(root.open_file(rel, os.O_RDONLY))
        elif kind == "listdir":
            root.listdir(rel)
        elif kind == "stat":
            root.stat(rel)
    except (FsopsError, OSError):
        pass  # refusing is always acceptable; anything else is a bug and propagates


STRATEGIES = [Strategy.OPENAT2, Strategy.FALLBACK]
Ops = list[tuple[str, object, object]]


@st.composite
def attacks(draw: st.DrawFn) -> Ops:
    """Plants first (links at one or two levels), then arbitrary operations through them."""
    plants = draw(
        st.lists(
            st.tuples(
                st.just("plant"),
                st.lists(st.sampled_from(NAMES), min_size=1, max_size=2).map("/".join),
                link_targets,
            ),
            min_size=1,
            max_size=3,
        )
    )
    rest = draw(st.lists(operations, min_size=1, max_size=20))
    return [*plants, *rest]


# Known escapes, so that the baseline does not depend on the search finding them.
KNOWN: list[Ops] = [
    [("plant", "link", "OUT"), ("write", "link/x.cbz", b"a")],
    [("plant", "a", "OUT"), ("mkdir", "a/new", True), ("unlink", "a/secret", None)],
    [("plant", "a", "OUT/d"), ("write", "a/secret", b"a"), ("rename", "a/secret", "b")],
    [("plant", "x.cbz", "OUT/secret"), ("write", "x.cbz", b"a"), ("truncate", "x.cbz", b"")],
    [("plant", "a", "PROC"), ("listdir", "a/tmp", None), ("write", "a/tmp/f", b"a")],
    [("mkdir", "a", False), ("plant", "a/b", "OUT"), ("rmdir", "a/b/d", None)],
]


@pytest.mark.req("ENF-SEC-012")
@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.value)
@settings(
    max_examples=150,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture, HealthCheck.too_slow],
)
@example(ops=KNOWN[0])
@example(ops=KNOWN[1])
@example(ops=KNOWN[2])
@example(ops=KNOWN[3])
@example(ops=KNOWN[4])
@example(ops=KNOWN[5])
@given(ops=attacks())
def test_fsops_never_escapes_root_with_planted_symlinks(
    tmp_path_factory: pytest.TempPathFactory,
    strategy: Strategy,
    ops: list[tuple[str, object, object]],
) -> None:
    if strategy is Strategy.OPENAT2 and not openat2_supported():
        pytest.skip("openat2 not available")
    root_dir, out = build_world(tmp_path_factory.mktemp("world"))
    before = snapshot(out)
    with ConfinedRoot.open(root_dir, strategy=strategy) as root:
        for op in ops:
            apply(root_dir, root, op, out)
            assert snapshot(out) == before, f"escaped after {op!r}"
    # Nothing the root holds is a regular file or directory that was reached through a link.
    for current, _dirs, files in os.walk(root_dir, followlinks=False):
        for name in files:
            path = Path(current) / name
            if not path.is_symlink():  # planted by the test itself
                assert path.stat().st_dev == root_dir.stat().st_dev


@pytest.mark.req("ENF-SEC-012")
@pytest.mark.parametrize("strategy", STRATEGIES, ids=lambda s: s.value)
def test_symlink_swapped_in_while_writing_never_lets_a_write_out(
    tmp_path: Path, strategy: Strategy
) -> None:
    """A second thread keeps replacing the parent directory by a link to the outside."""
    if strategy is Strategy.OPENAT2 and not openat2_supported():
        pytest.skip("openat2 not available")
    root_dir, out = build_world(tmp_path)
    before = snapshot(out)
    stop = threading.Event()
    (root_dir / "a").mkdir()

    def attacker() -> None:
        while not stop.is_set():
            with contextlib.suppress(OSError):
                (root_dir / "a" / "b").rmdir()
            with contextlib.suppress(OSError):
                os.symlink(out, root_dir / "a" / "b")
                (root_dir / "a" / "b").unlink()
                (root_dir / "a" / "b").mkdir()

    thread = threading.Thread(target=attacker, daemon=True)
    thread.start()
    try:
        with ConfinedRoot.open(root_dir, strategy=strategy) as root:
            for index in range(400):
                try:
                    root.mkdir("a/b", parents=True, exist_ok=True)
                    fd = root.open_file(f"a/b/f{index}", os.O_WRONLY | os.O_CREAT, 0o644)
                    os.close(fd)
                    root.unlink(f"a/b/f{index}")
                except (FsopsError, OSError):
                    continue
    finally:
        stop.set()
        thread.join()
    assert snapshot(out) == before
