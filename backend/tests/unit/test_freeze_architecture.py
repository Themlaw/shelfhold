"""M1b L02: `instance.restore_generation` is read and written by `FreezeState` alone (D11).

Any other module goes through it. The scan is by syntax, so a comment or a docstring that names
the column does not count, and a real access (attribute, name, keyword, string key) does."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
ALLOWED = {
    "infrastructure/freeze.py",  # the one reader and writer
    "infrastructure/schema/platform.py",  # the column's definition
}
COLUMN = "restore_generation"

pytestmark = pytest.mark.req("EF-INT-026")


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def accesses(source: str) -> list[int]:
    """Lines where the code names the column: attribute, identifier, keyword, string value."""
    tree = ast.parse(source)
    docs = _docstrings(tree)
    lines: list[int] = []
    for node in ast.walk(tree):
        named = (
            (isinstance(node, ast.Attribute) and node.attr == COLUMN)
            or (isinstance(node, ast.Name) and node.id == COLUMN)
            or (isinstance(node, ast.keyword) and node.arg == COLUMN)
            or (isinstance(node, ast.alias) and node.name == COLUMN)
            or (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and COLUMN in node.value
                and id(node) not in docs
            )
        )
        if named:
            lines.append(getattr(node, "lineno", 0))
    return lines


def violations(root: Path) -> list[str]:
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in ALLOWED or rel.startswith("infrastructure/migrations"):
            continue
        found += [f"{rel}:{n}" for n in accesses(path.read_text("utf-8"))]
    return found


def test_no_reader_of_restore_generation_outside_freeze_state() -> None:
    assert violations(SRC) == []


@pytest.mark.parametrize(
    "code",
    [
        "x = head.restore_generation",
        "sa.select(t.c.restore_generation)",
        "connection.execute(sa.update(t).values(restore_generation=0))",
        "sa.text('UPDATE instance SET restore_generation = 0')",
        "from m import restore_generation",
    ],
)
def test_the_scan_catches_each_form_of_access(code: str) -> None:
    assert accesses(code)


def test_the_scan_lets_a_comment_and_a_docstring_through() -> None:
    assert not accesses('def f():\n    """Reads restore_generation, says the old text."""\n')
    assert not accesses("x = 1  # restore_generation lives in FreezeState\n")


def test_the_scan_fails_on_a_violating_module(tmp_path: Path) -> None:
    (tmp_path / "ops").mkdir()
    (tmp_path / "ops" / "peek.py").write_text("def f(row):\n    return row.restore_generation\n")
    assert violations(tmp_path) == ["ops/peek.py:2"]
