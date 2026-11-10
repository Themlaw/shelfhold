"""EF-USR-024 / ENF-SEC-010 (passe 13, N2): every emitter of a permanent deletion is classed.

The scan reads the source of the whole package. Each function that deletes (`unlink`, `rmdir`,
`rmtree`, `os.remove`, `remove_tree`, `empty_dir`, an `FsOp` of kind unlink/delete, or that is
named purge / empty trash / detach) must be listed in `DELETION_SITES` with one class:

- a reserved action declared in `RESERVED_ACTIONS` (triggered by a credential),
- a name of the closed list `SYSTEM_EMITTERS`,
- `PRIMITIVE`: the fsops layer, whose callers are the classed emitters,
- `REPLAY`: the crash-recovery replay of the intent journal (empty folders already journaled),
- `OWN_SCRATCH`: a file the process made for its own mechanism (temporary, progress, lease).

A new emitter without a class, or a class without an emitter any more, fails this test.
"""

import ast
from pathlib import Path

import pytest

from shelfhold.identity.public import (
    CLOSED_SITES,
    DELETION_SITES,
    RESERVED_ACTIONS,
    SYSTEM_EMITTERS,
)

SRC = Path(__file__).resolve().parents[2] / "src"
SYSTEM_CALLS = {"system_unlink": 0, "system_remove_tree": 0, "_system_empty": 1}  # emitter arg
ATTRIBUTES = {
    "unlink", "rmdir", "rmtree", "remove_tree", "empty_dir", "removedirs", *SYSTEM_CALLS,
}  # fmt: skip
FS_OPS = {"unlink", "rmdir", "delete", "purge_upstream"}
NAMED = ("purge", "empty_trash", "detach")
LIBRARIES = {"os", "shutil", "pathlib", "Path"}
IMPORTED = ATTRIBUTES | {"remove", "move"}


def _is_deletion_name(name: str) -> bool:
    return name in ATTRIBUTES


class _Scanner(ast.NodeVisitor):
    def __init__(self, module: str) -> None:
        self.module = module
        self.stack: list[str] = []
        self.found: set[str] = set()
        self.claims: dict[str, list[str | None]] = {}  # site -> literal emitter per system call

    def _enter(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self.stack.append(node.name)
        if not isinstance(node, ast.ClassDef) and node.name.lstrip("_").startswith(NAMED):
            self.found.add(self._site())
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _enter

    def _site(self) -> str:
        return f"{self.module}::{'.'.join(self.stack) or '<module>'}"

    def visit_Attribute(self, node: ast.Attribute) -> None:
        # Any reference, called or aliased (`kill = Path.unlink`), not only a call.
        if node.attr in ATTRIBUTES or (
            node.attr == "remove" and isinstance(node.value, ast.Name) and node.value.id == "os"
        ):
            self.found.add(self._site())
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in ATTRIBUTES:
            self.found.add(self._site())

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (node.module or "").split(".")[0] in LIBRARIES and any(
            alias.name in IMPORTED for alias in node.names
        ):
            self.found.add(self._site())  # `from shutil import rmtree as rt` hides the call

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name) and func.id == "getattr" and len(node.args) >= 2:
            target, name = node.args[0], node.args[1]
            on_library = isinstance(target, ast.Name) and target.id in LIBRARIES
            literal = isinstance(name, ast.Constant) and isinstance(name.value, str)
            if on_library and (not literal or name.value in IMPORTED):  # type: ignore[attr-defined]
                self.found.add(self._site())  # `getattr(os, "unl" + "ink")` and the like
        if isinstance(func, ast.Attribute) and func.attr in SYSTEM_CALLS:
            index = SYSTEM_CALLS[func.attr]
            arg = node.args[index] if len(node.args) > index else None
            literal_emitter = (
                arg.value if isinstance(arg, ast.Constant) and isinstance(arg.value, str) else None
            )
            self.claims.setdefault(self._site(), []).append(literal_emitter)
        if any(
            kw.arg == "op" and isinstance(kw.value, ast.Constant) and kw.value.value in FS_OPS
            for kw in node.keywords
        ):
            self.found.add(self._site())
        self.generic_visit(node)


def scan() -> tuple[set[str], dict[str, list[str | None]]]:
    sites: set[str] = set()
    claims: dict[str, list[str | None]] = {}
    for path in sorted((SRC / "shelfhold").rglob("*.py")):
        module = path.relative_to(SRC).as_posix()
        scanner = _Scanner(module)
        scanner.visit(ast.parse(path.read_text(encoding="utf-8")))
        sites |= scanner.found
        claims |= scanner.claims
    return sites, claims


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_every_deletion_emitter_is_classified() -> None:
    found, _ = scan()
    unclassified = sorted(found - set(DELETION_SITES))
    assert not unclassified, f"deleting code without a class: {unclassified}"
    stale = sorted(set(DELETION_SITES) - found)
    assert not stale, f"classified emitters that no longer exist: {stale}"


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_each_class_is_a_declared_action_a_system_emitter_or_a_closed_kind() -> None:
    allowed = set(RESERVED_ACTIONS) | SYSTEM_EMITTERS | set(CLOSED_SITES)
    for site, kind in DELETION_SITES.items():
        assert kind in allowed, f"{site}: {kind!r} is none of the allowed classes"


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_a_system_call_names_its_emitter_by_literal_and_matches_the_site_class() -> None:
    _, claims = scan()
    assert claims, "the scan sees no system_unlink call at all"
    for site, emitters in claims.items():
        for emitter in emitters:
            assert emitter is not None, f"{site}: the emitter must be a string literal"
            assert emitter in SYSTEM_EMITTERS, f"{site}: {emitter!r} is not a system emitter"
            assert DELETION_SITES[site] == emitter, (
                f"{site} calls as {emitter!r} but is classed {DELETION_SITES[site]!r}"
            )


# Pinned by hand: a closed class lists exactly its admitted sites (module::function), and
# widening one is a diff in two files. Review PR #43 (M2).
FSOPS = "shelfhold/infrastructure/fsops"
EXPECTED_CLOSED = {
    "fsops_primitive": {
        f"{FSOPS}/confined.py::ConfinedRoot._empty",
        f"{FSOPS}/confined.py::ConfinedRoot._rename_noreplace",
        f"{FSOPS}/confined.py::ConfinedRoot.remove_tree",
        f"{FSOPS}/confined.py::ConfinedRoot.rmdir",
        f"{FSOPS}/confined.py::ConfinedRoot.unlink",
        f"{FSOPS}/facade.py::Fsops._system_empty",
        f"{FSOPS}/facade.py::Fsops.relocate_inbox",
        f"{FSOPS}/facade.py::Fsops.purge_remove_tree",
        f"{FSOPS}/facade.py::Fsops.purge_unlink",
        f"{FSOPS}/facade.py::Fsops.recycle",
        f"{FSOPS}/facade.py::Fsops.rmdir",
        f"{FSOPS}/facade.py::Fsops.system_remove_tree",
        f"{FSOPS}/facade.py::Fsops.system_unlink",
        f"{FSOPS}/facade.py::Fsops.unlink",
        "shelfhold/infrastructure/instance/lease.py::WriteSession.rmdir",
        "shelfhold/infrastructure/instance/lease.py::WriteSession.unlink",
    },
    "database_rows": {"shelfhold/acquisition/application/instance.py::_purge_orphans"},
    "own_scratch": {
        f"{FSOPS}/facade.py::_claim_lease",
        "shelfhold/infrastructure/migrate.py::MigrationStateFile.clear",
        "shelfhold/infrastructure/secrets/atomic.py::write_private",
        "shelfhold/infrastructure/local_socket/server.py::LocalSocketServer._remove_socket",
        "shelfhold/infrastructure/local_socket/server.py::bind_private",
        "shelfhold/plugin_host/install.py::install_wheel",
        "shelfhold/plugin_host/install.py::remove_plugin",
        "shelfhold/plugin_host/install.py::_undo",
        f"{FSOPS}/probe.py::_remove_probe_dir",
    },
    "backup_directory": {
        "shelfhold/ops/backup/store.py::BackupDirectory._discard",
        "shelfhold/ops/backup/scratch.py::_remove_scratch",
    },
    "restore": {"shelfhold/infrastructure/migrate.py::_restore_database"},
    "intent_replay": {"shelfhold/export/application/intents.py::IntentJournal._resolve_rmdir"},
}


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_each_closed_class_lists_exactly_its_admitted_sites() -> None:
    assert {name: set(sites) for name, sites in CLOSED_SITES.items()} == EXPECTED_CLOSED
    for name, sites in CLOSED_SITES.items():
        for site in sites:
            assert DELETION_SITES[site] == name


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_the_staging_cleanup_emitter_exists_in_code() -> None:
    # The other system emitters (probe file, trash retention, disk guard) arrive with their
    # lots (L21, M3); fsops already enforces their perimeters and conditions.
    assert "staging_orphan_cleanup" in set(DELETION_SITES.values())


def _scan_source(tmp_path: Path, source: str) -> _Scanner:
    path = tmp_path / "x.py"
    path.write_text(source, encoding="utf-8")
    scanner = _Scanner("x.py")
    scanner.visit(ast.parse(path.read_text(encoding="utf-8")))
    return scanner


@pytest.mark.req("EF-USR-024")
def test_the_scanner_sees_a_new_emitter(tmp_path: Path) -> None:
    scanner = _scan_source(
        tmp_path,
        "import shutil, os\nclass A:\n    def go(self, p):\n        shutil.rmtree(p)\n"
        "def other(p):\n    os.remove(p)\ndef purge_everything():\n    pass\n"
        "def plan():\n    return FsOp(op='unlink', path='a')\n",
    )
    assert scanner.found == {"x.py::A.go", "x.py::other", "x.py::purge_everything", "x.py::plan"}


@pytest.mark.req("EF-USR-024", "ENF-SEC-010")
def test_the_scanner_sees_the_system_calls_and_the_disguised_forms(tmp_path: Path) -> None:
    """Regression of the review PoCs (bypass_mod.py): none of these may go unseen."""
    source = (
        "import os\nimport shutil\nfrom pathlib import Path\n"
        "from os import remove\nfrom shutil import rmtree as rt\n"
        "def a(p): getattr(os, 'unl' + 'ink')(p)\n"
        "def b(p): rt(p)\n"
        "def c(p): remove(p)\n"
        "def d(p):\n    kill = Path.unlink\n    kill(Path(p))\n"
        "def e(f, p): f.system_remove_tree('trash_retention_purge', p)\n"
        "def f(f, p): f.system_unlink('probe_file_cleanup', p)\n"
        "def g(f, r): f._system_empty(r, 'staging_orphan_cleanup', 'x')\n"
        "def h(p): getattr(shutil, 'rmtree')(p)\n"
        "def i(f, p, name): f.system_unlink(name, p)\n"
    )
    scanner = _scan_source(tmp_path, source)
    for name in "adefghi":
        assert f"x.py::{name}" in scanner.found, name
    # An aliased import (`rt`, `remove`) is flagged at the import itself, wherever it is used.
    assert "x.py::<module>" in scanner.found
    assert scanner.claims["x.py::e"] == ["trash_retention_purge"]
    assert scanner.claims["x.py::g"] == ["staging_orphan_cleanup"]
    assert scanner.claims["x.py::i"] == [None]  # not a literal: the claims test refuses it
