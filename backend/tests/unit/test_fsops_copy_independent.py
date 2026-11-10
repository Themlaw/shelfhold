"""EF-BIB-027: a kept copy is never a hard link; the fsops write path has no second name."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
# `os.link` survives in three places, none of them a way to keep a copy: the no-clobber fallback of
# a rename on a kernel without renameat2, the capability probe that asks the file system whether
# it could link (EF-EXP-015, M2), and the private-file writer of the keyring (not a library file).
ALLOWED = {
    "infrastructure/fsops/confined.py": {"_rename_noreplace", "link", "link_to"},
    "infrastructure/secrets/atomic.py": {"write_private"},
    "ops/backup/store.py": {"write"},  # the archive is linked into place without replacing
}


def link_calls(path: Path) -> list[tuple[str, int]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[str, int]] = []

    def walk(node: ast.AST, function: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = child.name if isinstance(child, ast.FunctionDef) else function
            if (
                isinstance(child, ast.Attribute)
                and child.attr == "link"
                and isinstance(child.value, ast.Name)
                and child.value.id == "os"
            ):
                found.append((function, child.lineno))
            walk(child, name)

    walk(tree, "<module>")
    return found


@pytest.mark.req("EF-BIB-027")
def test_fsops_never_hardlinks() -> None:
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        for function, line in link_calls(path):
            if function not in ALLOWED.get(rel, set()):
                offenders.append(f"{rel}:{line} in {function}")
    assert offenders == []  # no os.link anywhere else in src


@pytest.mark.req("EF-BIB-027")
def test_the_write_path_of_a_copy_places_by_rename_not_by_link() -> None:
    writer = ast.parse((SRC / "infrastructure/fsops/writer.py").read_text(encoding="utf-8"))
    called = {
        n.func.attr
        for n in ast.walk(writer)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
    }
    assert "link" not in called and "link_to" not in called
    assert "rename" in called
