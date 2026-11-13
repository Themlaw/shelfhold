"""Every `Fsops` opened by the application says where the database's generation is read.

An `Fsops` that lives long (an export waiting for its scan) holds the generation it read when it
opened. Without a source it would read a sentinel another task moved as `restored_behind`. The
scan is by syntax: a call to `open_fsops(...)`, `Fsops.open(...)` or `Fsops(...)` outside the
tests passes a `generation_source` keyword that is not the literal `None`."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
# The definitions themselves (the classmethod forwards what it was given).
DEFINITIONS = {"infrastructure/fsops/facade.py"}

pytestmark = pytest.mark.req("EF-EXP-029", "EF-INT-026")


def _opens_fsops(call: ast.Call) -> bool:
    """`open_fsops(...)`, `Fsops(...)` or `Fsops.open(...)`; `Composition.open_fsops` supplies
    its own source and is not a call this scan wants."""
    function = call.func
    if isinstance(function, ast.Name):
        return function.id in {"open_fsops", "Fsops"}
    return (
        isinstance(function, ast.Attribute)
        and function.attr == "open"
        and isinstance(function.value, ast.Name)
        and function.value.id == "Fsops"
    )


def _calls_without_source() -> list[str]:
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        if relative in DEFINITIONS:
            continue
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            if not isinstance(node, ast.Call) or not _opens_fsops(node):
                continue
            keyword = next((k for k in node.keywords if k.arg == "generation_source"), None)
            if keyword is None or (
                isinstance(keyword.value, ast.Constant) and keyword.value.value is None
            ):
                found.append(f"{relative}:{node.lineno}")
    return found


def test_every_fsops_opened_outside_the_tests_has_a_generation_source() -> None:
    assert _calls_without_source() == []


def test_the_scan_sees_a_call_without_a_source() -> None:
    tree = ast.parse("open_fsops(loaded, umask=0)\nFsops.open(a, b, generation_source=None)\n")
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call) and _opens_fsops(n)]
    assert len(calls) == 2
