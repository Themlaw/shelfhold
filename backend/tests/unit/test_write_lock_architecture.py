"""No transaction of the production code starts deferred, unless it is listed here, with its reason.

SQLite refuses at once (`database is locked`, no busy timeout) a deferred transaction that reads
and then writes when another writer committed in between. The rule is by syntax:

- a `.begin()` call is a write transaction: it comes from `write_transaction(engine)` (which takes
  the write lock first), or its function sets `shelfhold_begin="BEGIN IMMEDIATE"` on the
  connection it begins;
- a function that opens a connection and commits on it does the same;
- a transaction that never writes may stay deferred: it is in `DEFERRED_READS`, by file and
  function, with the reason. An entry that matches nothing is an error (the list does not rot)."""

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
LOCK = "shelfhold_begin"

pytestmark = pytest.mark.req("ENF-OPS-003")

# (file, function) -> why it never writes.
DEFERRED_READS: dict[tuple[str, str], str] = {
    ("acquisition/application/ranges.py", "RangeTickets._retry"): (
        "reads the attempts of a request and writes nothing; the write that follows is another "
        "transaction"
    ),
}


def _functions(tree: ast.AST) -> list[tuple[str, ast.AST]]:
    found: list[tuple[str, ast.AST]] = []

    def visit(node: ast.AST, prefix: str) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, f"{prefix}{child.name}.")
            elif isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                found.append((f"{prefix}{child.name}", child))
                visit(child, f"{prefix}{child.name}.")
            else:
                visit(child, prefix)

    visit(tree, "")
    return found


def _own_nodes(function: ast.AST) -> list[ast.AST]:
    """The nodes of a function, without those of the functions nested in it."""
    nodes: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(function))
    while stack:
        node = stack.pop()
        nodes.append(node)
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            stack.extend(ast.iter_child_nodes(node))
    return nodes


def _calls(nodes: list[ast.AST], attribute: str) -> list[ast.Call]:
    return [
        n
        for n in nodes
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == attribute
        and not n.args
    ]


def _takes_the_lock(source: str, function: ast.AST) -> bool:
    segment = ast.get_source_segment(source, function) or ""
    return LOCK in segment and "BEGIN IMMEDIATE" in segment


def _locked_receiver(source: str, call: ast.Call) -> bool:
    """`connection.begin()` after `connection.execution_options(shelfhold_begin=...)`, or a
    receiver that is itself the engine with the option; never a bare `engine.begin()`."""
    receiver = call.func.value  # type: ignore[attr-defined]
    text = (ast.get_source_segment(source, receiver) or "").lower()
    return (isinstance(receiver, ast.Name) and not text.endswith("engine")) or "immediate" in text


def _violations() -> tuple[list[str], set[tuple[str, str]]]:
    bad: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        source = path.read_text("utf-8")
        for name, function in _functions(ast.parse(source)):
            nodes = _own_nodes(function)
            begins = _calls(nodes, "begin")
            writes_on_a_connection = _calls(nodes, "connect") and _calls(nodes, "commit")
            if not (begins or writes_on_a_connection):
                continue
            if _takes_the_lock(source, function) and all(
                _locked_receiver(source, call) for call in begins
            ):
                continue
            if (relative, name) in DEFERRED_READS:
                used.add((relative, name))
                continue
            line = (begins or _calls(nodes, "connect"))[0].lineno
            bad.append(f"{relative}:{line} {name}")
    return bad, used


def test_every_transaction_that_may_write_takes_the_write_lock_first() -> None:
    bad, _used = _violations()
    assert not bad, (
        "deferred transaction (use `write_transaction(engine)` from shelfhold.infrastructure.db, "
        "or list a read-only one in DEFERRED_READS):\n" + "\n".join(bad)
    )


def test_the_deferred_list_holds_only_live_entries_with_a_reason() -> None:
    _bad, used = _violations()
    assert set(DEFERRED_READS) == used
    assert all(reason.strip() for reason in DEFERRED_READS.values())
