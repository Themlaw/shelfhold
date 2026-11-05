"""EF-USR-024 / ENF-SEC-010 (passe 13, N2): every emitter of a permanent deletion is classed.

The scan reads the source of the whole package. Each function that deletes (`unlink`, `rmdir`,
`rmtree`, `os.remove`, `remove_tree`, `empty_dir`, an `FsOp` of kind unlink/delete, or that is
named purge / empty trash / detach) must be listed in `DELETION_SITES` with one class:

- a reserved action declared in `RESERVED_ACTIONS` (triggered by a credential),
- a name of the closed list `SYSTEM_EMITTERS`,
- `PRIMITIVE`: the fsops layer, whose callers are the classed emitters,
- `OWN_SCRATCH`: a file the process made for its own mechanism (temporary, progress, lease).

A new emitter without a class, or a class without an emitter any more, fails this test.
"""

import ast
from pathlib import Path

import pytest

from shelfhold.identity.public import (
    DELETION_SITES,
    OWN_SCRATCH,
    PRIMITIVE,
    RESERVED_ACTIONS,
    SYSTEM_EMITTERS,
)

SRC = Path(__file__).resolve().parents[2] / "src"
ATTRIBUTES = {"unlink", "rmdir", "rmtree", "remove_tree", "empty_dir", "removedirs"}
FS_OPS = {"unlink", "rmdir", "delete", "purge_upstream"}
NAMED = ("purge", "empty_trash", "detach")
SCRATCH_MODULES = {
    "shelfhold/infrastructure/migrate.py",
    "shelfhold/infrastructure/secrets/atomic.py",
    "shelfhold/infrastructure/fsops/facade.py",
}


def _is_deletion(call: ast.Call) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute):
        if func.attr in ATTRIBUTES:
            return True
        if func.attr == "remove" and isinstance(func.value, ast.Name) and func.value.id == "os":
            return True
    if isinstance(func, ast.Name) and func.id in ATTRIBUTES:
        return True
    return any(
        kw.arg == "op" and isinstance(kw.value, ast.Constant) and kw.value.value in FS_OPS
        for kw in call.keywords
    )


class _Scanner(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.stack: list[str] = []
        self.found: set[str] = set()

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.stack.append(node.name)
        if not isinstance(node, ast.ClassDef) and node.name.lstrip("_").startswith(NAMED):
            self.found.add(self._site())
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter  # noqa: N815

    def _site(self) -> str:
        return f"{self.module}::{'.'.join(self.stack) or '<module>'}"

    def visit_Call(self, node: ast.Call) -> None:
        if _is_deletion(node):
            self.found.add(self._site())
        self.generic_visit(node)


def scan() -> set[str]:
    sites: set[str] = set()
    for path in sorted((SRC / "shelfhold").rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        scanner = _Scanner(module)
        scanner.visit(ast.parse(path.read_text(encoding="utf-8")))
        sites |= scanner.found
    return sites


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_every_deletion_emitter_is_classified() -> None:
    found = scan()
    unclassified = sorted(found - set(DELETION_SITES))
    assert not unclassified, f"deleting code without a class (reserved action or system emitter): {unclassified}"
    stale = sorted(set(DELETION_SITES) - found)
    assert not stale, f"classified emitters that no longer exist: {stale}"


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_each_class_is_a_declared_action_a_system_emitter_or_one_of_two_closed_kinds() -> None:
    allowed = set(RESERVED_ACTIONS) | SYSTEM_EMITTERS | {PRIMITIVE, OWN_SCRATCH}
    for site, kind in DELETION_SITES.items():
        assert kind in allowed, f"{site}: {kind!r} is none of the allowed classes"


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_primitive_and_scratch_classes_stay_where_they_belong() -> None:
    for site, kind in DELETION_SITES.items():
        module = site.split("::")[0]
        if kind == PRIMITIVE:
            assert module.startswith("shelfhold/infrastructure/fsops/") or module.endswith(
                "infrastructure/instance/lease.py"
            ), site
        if kind == OWN_SCRATCH:
            assert module in SCRATCH_MODULES, site


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_the_staging_cleanup_emitter_exists_in_code() -> None:
    # The other system emitters (probe file, trash retention, disk guard) arrive with their
    # lots (L21, L26+); their perimeters are already enforced by fsops (see test_fsops_emitter_*).
    assert "staging_orphan_cleanup" in set(DELETION_SITES.values())


@pytest.mark.req("EF-USR-024")
def test_the_scanner_sees_a_new_emitter(tmp_path: Path) -> None:
    source = tmp_path / "x.py"
    source.write_text(
        "import shutil, os\nclass A:\n    def go(self, p):\n        shutil.rmtree(p)\n"
        "def other(p):\n    os.remove(p)\ndef purge_everything():\n    pass\n"
        "def plan():\n    return FsOp(op='unlink', path='a')\n",
        encoding="utf-8",
    )
    scanner = _Scanner("x.py")
    scanner.visit(ast.parse(source.read_text(encoding="utf-8")))
    assert scanner.found == {"x.py::A.go", "x.py::other", "x.py::purge_everything", "x.py::plan"}
