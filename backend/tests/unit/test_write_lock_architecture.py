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
    ("interfaces/http_api/idempotency.py", "IdempotencyMiddleware.__call__"): (
        "`self.store.begin` is the idempotency store's own method (it claims a key), handed to "
        "the thread pool: not a database transaction"
    ),
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
    """The names of a scope, each with its bindings in the order of the source: what a name is at
    a line is what it was last bound to before that line. A name bound to a locked connection and
    later to an unlocked one is unlocked from there on, with no need to rename it."""

    name: str
    node: ast.AST
    parent: "_Scope | None"
    kinds: dict[str, list[tuple[int, str]]] = field(default_factory=dict)

    def bind(self, name: str, line: int, kind: str) -> None:
        self.kinds.setdefault(name, []).append((line, kind))

    def lookup(self, name: str, line: int) -> str | None:
        if name in self.kinds:
            before = [kind for at, kind in self.kinds[name] if at <= line]
            return before[-1] if before else None
        # a name of an enclosing scope (a closure): only when all its bindings say the same
        scope = self.parent
        while scope is not None:
            if name in scope.kinds:
                found = {kind for _at, kind in scope.kinds[name]}
                return found.pop() if len(found) == 1 else None
            scope = scope.parent
        return None


def _kind_of(expr: ast.AST, scope: _Scope) -> str | None:
    """What `expr` is, as far as the locking goes."""
    if isinstance(expr, ast.Name):
        return scope.lookup(expr.id, expr.lineno)
    if _is_locking_call(expr):
        return _LOCKED_ENGINE
    name = _call_name(expr)
    if name == "write_transaction":
        return _LOCKED_CONN
    if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Attribute):
        return None
    if name == "enter_context" and expr.args:  # `ExitStack`: what it opens is what it hands back
        return _kind_of(expr.args[0], scope)
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
                scope.bind(arg.arg, 0, _PARAM)
    events: list[tuple[int, int, ast.AST, str | None]] = []
    for n in _own_nodes(node):
        targets: list[tuple[ast.AST, ast.AST]] = []
        if isinstance(n, ast.Assign) and len(n.targets) == 1:
            targets.append((n.targets[0], n.value))
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            targets.append((n.target, n.value))
        elif isinstance(n, ast.With | ast.AsyncWith):
            targets.extend((i.optional_vars, i.context_expr) for i in n.items if i.optional_vars)
        for target, value in targets:
            if isinstance(target, ast.Name):
                events.append((n.lineno, target.col_offset, value, target.id))
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Call):
            call = n.value
            receiver = call.func.value if isinstance(call.func, ast.Attribute) else None
            if isinstance(receiver, ast.Name) and (
                _is_locking_call(call) or _begins_immediate(call)
            ):  # `connection.execution_options(shelfhold_begin=...)`: locks it in place
                events.append((n.lineno, n.col_offset, call, receiver.id))
    for line, _col, value, name in sorted(events, key=lambda e: (e[0], e[1])):
        assert name is not None
        if isinstance(value, ast.Call) and (_is_locking_call(value) or _begins_immediate(value)):
            receiver = value.func.value  # type: ignore[attr-defined]
            if isinstance(receiver, ast.Name) and receiver.id == name:
                scope.bind(name, line, _LOCKED_CONN)  # the in-place form
                continue
        kind = _kind_of_at(value, scope, line)
        if kind is not None:
            scope.bind(name, line, kind)


def _kind_of_at(expr: ast.AST, scope: _Scope, line: int) -> str | None:
    """`_kind_of` for a binding made at `line`: its names are those bound before it."""
    if isinstance(expr, ast.Name):
        return scope.lookup(expr.id, line)
    if not isinstance(expr, ast.Call) or not isinstance(expr.func, ast.Attribute):
        return _kind_of(expr, scope)
    inner = expr.func.value
    if isinstance(inner, ast.Name) and expr.func.attr in ("connect", "begin", "begin_nested"):
        receiver = scope.lookup(inner.id, line)
        if expr.func.attr == "connect" and inner.id != "sqlite3":
            return _LOCKED_CONN if receiver == _LOCKED_ENGINE else _CONN
        if expr.func.attr == "begin" and receiver in (_LOCKED_ENGINE, _LOCKED_CONN):
            return _LOCKED_CONN
        if expr.func.attr in ("begin", "begin_nested"):
            return _TXN
    return _kind_of(expr, scope)


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


def _refusal(node: ast.AST, scope: _Scope, called: set[int]) -> str | None:
    """Why `node` starts or ends a transaction that may be deferred, or None."""
    if (
        isinstance(node, ast.Attribute)
        and node.attr in ("begin", "commit")
        and id(node) not in called
    ):
        return f"{ast.unparse(node)} taken as a value"  # `begin = engine.begin`
    if isinstance(node, ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) > 1
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value in ("begin", "commit")
        ):
            return f"{ast.unparse(node)} (the call is hidden)"
        if isinstance(node.func, ast.Attribute) and node.func.attr in ("begin", "commit"):
            if node.args:
                return None
            kind = _kind_of(node.func.value, scope)
            if node.func.attr == "begin":
                refused = kind not in (_LOCKED_ENGINE, _LOCKED_CONN)
            else:  # a commit: of a connection that may write, not of a transaction object
                refused = kind != _TXN and kind not in (_LOCKED_ENGINE, _LOCKED_CONN)
            return ast.unparse(node.func) if refused else None
    if isinstance(node, ast.With | ast.AsyncWith):
        for item in node.items:
            call = item.context_expr
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "connect"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "sqlite3"
                and not any(
                    k.arg == "isolation_level"
                    and isinstance(k.value, ast.Constant)
                    and k.value.value is None
                    for k in call.keywords
                )
            ):  # it commits when the block ends, and its BEGIN is deferred
                return "with sqlite3.connect(...) commits a deferred transaction"
    return None


def _scan(
    root: Path, allowed: dict[tuple[str, str], str]
) -> tuple[list[str], set[tuple[str, str]]]:
    bad: list[str] = []
    used: set[tuple[str, str]] = set()
    for path in sorted(root.rglob("*.py")) if root.is_dir() else [root]:
        relative = path.relative_to(root if root.is_dir() else root.parent).as_posix()
        tree = ast.parse(path.read_text("utf-8"))
        for name, scope in _scopes(tree):
            nodes = list(_own_nodes(scope.node))
            called = {id(n.func) for n in nodes if isinstance(n, ast.Call)}
            for node in nodes:
                found = _refusal(node, scope, called)
                if found is None:
                    continue
                if (relative, name) in allowed:
                    used.add((relative, name))
                    continue
                bad.append(f"{relative}:{node.lineno} {name} ({found})")
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
