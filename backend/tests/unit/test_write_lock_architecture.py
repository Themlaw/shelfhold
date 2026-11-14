"""No transaction of the production code starts deferred, unless it is listed here, with its reason.

SQLite refuses at once (`database is locked`, no busy timeout) a deferred transaction that reads
and then writes when another writer committed in between. The rule is by syntax, and it follows
the object the call is made on, not the words around it:

- `X.begin()` is accepted only when `X` is provably locked: `write_transaction(...)`, an engine
  made with `execution_options(shelfhold_begin="BEGIN IMMEDIATE")`, or a connection opened from
  one (or given that option);
- `X.commit()` on a connection that is not provably locked (opened by `engine.connect()` or
  `sqlite3.connect(...)`, whatever its arguments, or received as an argument) is refused: the
  helper that commits for a caller is a deferred write too;
- module-level code is looked at like a function body;
- a read-only transaction goes through `read_transaction`, which is not a `.begin()`.

The helpers of the tests (`tests/*_env.py`) follow the same rule. The few exceptions are in
`DEFERRED_READS`, by file and function, with the reason; an entry that matches nothing is an
error."""

import ast
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"
TESTS = Path(__file__).resolve().parents[1]
LOCK = "shelfhold_begin"

pytestmark = pytest.mark.req("ENF-OPS-003")

# (file, function) -> why it may start deferred.
DEFERRED_READS: dict[tuple[str, str], str] = {
    ("infrastructure/db.py", "foreign_keys_disabled"): (
        "its commit ends the transaction the caller opened (always a locked connection) before "
        "a PRAGMA that SQLite ignores inside one; it writes nothing itself"
    ),
}
# Names in tests that are allowed to stay deferred: none (the helpers seed under the lock).
DEFERRED_TESTS: dict[tuple[str, str], str] = {}

_LOCKED_ENGINE, _LOCKED_CONN, _CONN, _RAW, _TXN, _PARAM = (
    "locked_engine", "locked_conn", "conn", "raw", "txn", "param",
)  # fmt: skip


def _is_locking_call(node: ast.AST) -> bool:
    """`X.execution_options(shelfhold_begin="BEGIN IMMEDIATE")`."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "execution_options"
        and any(
            k.arg == LOCK
            and isinstance(k.value, ast.Constant)
            and k.value.value == "BEGIN IMMEDIATE"
            for k in node.keywords
        )
    )


def _begins_immediate(node: ast.Call) -> bool:
    """`connection.execute("BEGIN IMMEDIATE")` on a driver connection."""
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "execute"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and str(node.args[0].value).upper().startswith("BEGIN IMMEDIATE")
    )


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
    return None


@dataclass
class _Scope:
    name: str
    node: ast.AST
    parent: "_Scope | None"
    kinds: dict[str, str] = field(default_factory=dict)

    def lookup(self, name: str) -> str | None:
        scope: _Scope | None = self
        while scope is not None:
            if name in scope.kinds:
                return scope.kinds[name]
            scope = scope.parent
        return None


def _kind_of(expr: ast.AST, scope: _Scope) -> str | None:
    """What `expr` is, as far as the locking goes."""
    if isinstance(expr, ast.Name):
        return scope.lookup(expr.id)
    if _is_locking_call(expr):
        return _LOCKED_ENGINE
    name = _call_name(expr)
    if name == "write_transaction":
        return _LOCKED_CONN
    if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Attribute):
        return None
    receiver = _kind_of(expr.func.value, scope)
    if name == "connect":
        if isinstance(expr.func.value, ast.Name) and expr.func.value.id == "sqlite3":
            return _RAW
        return _LOCKED_CONN if receiver == _LOCKED_ENGINE else _CONN
    if name == "begin" and receiver in (_LOCKED_ENGINE, _LOCKED_CONN):
        return _LOCKED_CONN  # `with locked.begin() as connection`
    if name in ("begin", "begin_nested"):
        return _TXN
    return None


def _own_nodes(scope_node: ast.AST) -> Iterator[ast.AST]:
    """The nodes of a scope, without those of the functions and classes nested in it."""
    stack = list(ast.iter_child_nodes(scope_node))
    while stack:
        node = stack.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            stack.extend(ast.iter_child_nodes(node))


def _bind(scope: _Scope) -> None:
    node = scope.node
    if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
        args = node.args
        for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
            if arg is not None:
                scope.kinds[arg.arg] = _PARAM
    pending = [n for n in _own_nodes(node)]
    # two passes: a name bound from another name needs that name first
    for _ in range(2):
        for n in pending:
            targets: list[tuple[ast.AST, ast.AST]] = []
            if isinstance(n, ast.Assign) and len(n.targets) == 1:
                targets.append((n.targets[0], n.value))
            elif isinstance(n, ast.AnnAssign) and n.value is not None:
                targets.append((n.target, n.value))
            elif isinstance(n, ast.With | ast.AsyncWith):
                targets.extend(
                    (i.optional_vars, i.context_expr) for i in n.items if i.optional_vars
                )
            for target, value in targets:
                if isinstance(target, ast.Name):
                    kind = _kind_of(value, scope)
                    if kind is not None:
                        scope.kinds[target.id] = kind
    for n in pending:  # `connection.execution_options(shelfhold_begin=...)` locks it in place
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            call = n.value
            receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
            if isinstance(receiver, ast.Name) and (
                _is_locking_call(call) or _begins_immediate(call)
            ):
                scope.kinds[receiver.id] = _LOCKED_CONN


def _scopes(tree: ast.Module) -> Iterator[tuple[str, _Scope]]:
    def visit(node: ast.AST, parent: _Scope | None, prefix: str) -> Iterator[tuple[str, _Scope]]:
        name = prefix.rstrip(".") or "<module>"
        scope = _Scope(name, node, parent)
        _bind(scope)
        yield name, scope
        stack = list(ast.iter_child_nodes(node))
        while stack:
            child = stack.pop()
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                yield from visit(child, scope, f"{prefix}{child.name}.")
            elif isinstance(child, ast.Lambda):
                yield from visit(child, scope, f"{prefix}<lambda>.")
            else:
                stack.extend(ast.iter_child_nodes(child))

    yield from visit(tree, None, "")


def _scan(
    root: Path, allowed: dict[tuple[str, str], str]
) -> tuple[list[str], set[tuple[str, str]]]:
    bad: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")) if root.is_dir() else [root]:
        relative = path.relative_to(root if root.is_dir() else root.parent).as_posix()
        tree = ast.parse(path.read_text("utf-8"))
        for name, scope in _scopes(tree):
            for node in _own_nodes(scope.node):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("begin", "commit")
                    and not node.args
                ):
                    continue
                receiver = node.func.value
                kind = _kind_of(receiver, scope)
                if node.func.attr == "begin":
                    refused = kind not in (_LOCKED_ENGINE, _LOCKED_CONN)
                else:  # a commit: of a connection that may write, not of a transaction object
                    refused = kind in (_CONN, _RAW, _PARAM)
                if not refused:
                    continue
                if (relative, name) in allowed:
                    used.add((relative, name))
                    continue
                bad.append(f"{relative}:{node.lineno} {name} ({ast.unparse(node.func)})")
    return bad, used


def _violations() -> tuple[list[str], set[tuple[str, str]]]:
    return _scan(SRC, DEFERRED_READS)


def _test_helpers() -> list[Path]:
    return sorted(TESTS.glob("*_env.py"))


def _helper_violations() -> tuple[list[str], set[tuple[str, str]]]:
    bad: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in _test_helpers():
        found, seen = _scan(path, DEFERRED_TESTS)
        bad += [f"tests/{line}" for line in found]
        used |= {(path.name, name) for _file, name in seen}
    return bad, used


def test_every_transaction_that_may_write_takes_the_write_lock_first() -> None:
    bad, _used = _violations()
    assert not bad, (
        "deferred transaction (use `write_transaction(engine)` from shelfhold.infrastructure.db, "
        "`read_transaction` for a read, or list it in DEFERRED_READS with its reason):\n"
        + "\n".join(bad)
    )


def test_the_helpers_of_the_tests_seed_under_the_write_lock_too() -> None:
    bad, _used = _helper_violations()
    assert not bad, "deferred transaction in a test helper:\n" + "\n".join(bad)


def test_the_deferred_list_holds_only_live_entries_with_a_reason() -> None:
    _bad, used = _violations()
    assert set(DEFERRED_READS) == used
    assert all(reason.strip() for reason in DEFERRED_READS.values())
