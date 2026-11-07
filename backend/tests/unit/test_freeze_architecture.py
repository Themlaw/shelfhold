"""M1b L02: `instance.restore_generation` is read and written by `FreezeState` alone (D11).

Any other module goes through it. The scan is by syntax, so a comment or a docstring that names
the column does not count, and a real access (attribute, name, keyword, string key) does."""

import ast
import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
ALLOWED = {
    "infrastructure/freeze.py",  # the one reader and writer
    "infrastructure/schema/platform.py",  # the column's definition
}
COLUMN = "restore_generation"
# Readers of the whole `instance` row (select(instance), SELECT *) outside FreezeState. Explicit
# and empty: the row carries the column, so a reader of it reads the freeze.
WHOLE_ROW_READERS: set[str] = set()
INSTANCE_SQL = re.compile(r"\b(from|join)\s+instance\b|\binstance\s+set\b", re.IGNORECASE)

pytestmark = pytest.mark.req("EF-INT-026")


def _docstrings(tree: ast.AST) -> set[int]:
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                found.add(id(first.value))
    return found


def _names_instance(node: ast.AST) -> bool:
    """`instance`, `platform.instance`, `x.platform.instance`: the table object."""
    return (isinstance(node, ast.Name) and node.id == "instance") or (
        isinstance(node, ast.Attribute) and node.attr == "instance"
    )


def _mentions_instance(node: ast.AST) -> bool:
    return any(_names_instance(n) for n in ast.walk(node))


def accesses(source: str, *, whole_row_ok: bool = False) -> list[int]:
    """Lines where the code reaches the column, directly or around the name: attribute,
    identifier, keyword, string value, positional `instance.c[...]`, a `getattr` whose name is not
    a literal on the table, and (unless listed) the whole row of the table."""
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
                and id(node) not in docs
                and (COLUMN in node.value or (not whole_row_ok and INSTANCE_SQL.search(node.value)))
            )
            # instance.c["..."] / instance.c[i]: the name is hidden in an index
            or (
                isinstance(node, ast.Subscript)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "c"
                and _names_instance(node.value.value)
            )
            # getattr(instance.c, name) / getattr(instance, name) with a name not written out
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and not isinstance(node.args[1], ast.Constant)
                and _mentions_instance(node.args[0])
            )
            # select(instance) / select(*instance.c): every column of the table, the freeze included
            or (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute | ast.Name)
                and getattr(node.func, "attr", getattr(node.func, "id", "")) == "select"
                and not whole_row_ok
                and any(
                    _names_instance(a)
                    or (isinstance(a, ast.Starred) and _mentions_instance(a.value))
                    for a in node.args
                )
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
        found += [
            f"{rel}:{n}"
            for n in accesses(path.read_text("utf-8"), whole_row_ok=rel in WHOLE_ROW_READERS)
        ]
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
        "platform.instance.c['restore_' + 'generation']",
        "platform.instance.c[3]",
        "getattr(platform.instance.c, name)",
        "getattr(instance, key)",
        "sa.select(platform.instance)",
        "sa.select(*platform.instance.c)",
        "sa.text('SELECT * FROM instance')",
        "sa.text('select a from  instance where 1')",
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


def test_the_whole_row_list_lets_an_explicit_reader_through() -> None:
    assert accesses("sa.select(platform.instance)", whole_row_ok=True) == []


def test_the_scan_keeps_ordinary_reads_of_other_columns_and_tables() -> None:
    clean = (
        "sa.select(platform.instance.c.instance_id)\n"
        "getattr(row, name)\n"
        "sa.text('SELECT 1 FROM instance_lock')\n"
    )
    assert accesses(clean) == []
