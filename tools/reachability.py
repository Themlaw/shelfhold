#!/usr/bin/env python3
"""Production reachability of the symbols that `req` tests cite (M1a L58).

Standard library only. A test labelled `req` can pass while the production code never calls
the part it exercises ("traced, not wired"). This module builds a name-level reference graph
of `backend/src/shelfhold` and walks it from the real entry points; a `shelfhold.*` symbol
cited by a `req` test that the walk does not reach is an orphan, unless it is listed in
`tools/production-reachability-allowlist.toml` (reason, `until`).

Method (limits in docs/decisions/M1a-L58-reachability.md)
- Nodes: every module, top-level function/class/constant, and every method / class attribute.
- Edges: a definition references the names used in its body (nested functions and lambdas
  included). A bare name is resolved through the imports of its module; an attribute (`x.foo`)
  is resolved by NAME to every method or class attribute called `foo` (the dependency
  injection of the composition root is duck-typed, so no type is known statically).
- Roots: the composition root (`shelfhold.main`), the CLI (`shelfhold.interfaces.cli`, whose
  command groups are discovered by `register`), the Alembic migrations, and every
  module with a `__main__` guard. Huey handlers and routes are registered inside functions
  that the composition root reaches, so they need no root of their own.
- A reachable class makes its dunder methods and its attributes reachable (dataclass fields,
  enum members); a reachable module makes its module-level statements reachable.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

PACKAGE = "shelfhold"
ALLOWLIST = "tools/production-reachability-allowlist.toml"
SRC = "backend/src"
TESTS = "backend/tests"
# `until`: an M1b lot (L00..L32 of PLAN-M1b) or a later milestone.
UNTIL_RE = re.compile(r"^(M1b L(0\d|[12]\d|3[0-2])|M(?:[2-9]|10))$")

_ROOT_MODULES = ("shelfhold.main", "shelfhold.interfaces.cli.registry")
_ROOT_PREFIXES = ("shelfhold.infrastructure.migrations",)
_CLI_PACKAGE = "shelfhold.interfaces.cli"


@dataclass(frozen=True)
class Symbol:
    module: str
    qualname: str  # "" for the module itself

    @property
    def key(self) -> str:
        return f"{self.module}:{self.qualname}" if self.qualname else self.module

    @property
    def simple(self) -> str:
        return self.qualname.rsplit(".", 1)[-1] if self.qualname else self.module.rsplit(".", 1)[-1]


class Graph:
    def __init__(self) -> None:
        self.defs: dict[str, Symbol] = {}
        self.by_simple: dict[str, set[str]] = defaultdict(set)  # simple name -> keys
        self.members: dict[str, set[str]] = defaultdict(set)  # simple name -> member keys
        self.edges: dict[str, set[str]] = defaultdict(set)
        self.children: dict[str, set[str]] = defaultdict(set)  # class -> dunders, attributes
        self.modules: set[str] = set()
        self.imports: dict[str, dict[str, str]] = {}  # module -> local name -> target key
        self.roots: set[str] = set()

    # -- building ----------------------------------------------------------------------------

    def add_def(self, symbol: Symbol, *, member: bool = False) -> str:
        self.defs[symbol.key] = symbol
        self.by_simple[symbol.simple].add(symbol.key)
        if member:
            self.members[symbol.simple].add(symbol.key)
        return symbol.key


def module_name(src: Path, path: Path) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _names(node: ast.AST) -> tuple[set[str], set[str]]:
    """(bare names, attribute names) used anywhere under `node`."""
    bare: set[str] = set()
    attrs: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            bare.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            attrs.add(sub.attr)
    return bare, attrs


def _resolve_from(module: str, is_package: bool, node: ast.ImportFrom) -> str | None:
    if node.level == 0:
        return node.module
    base = module.split(".") if is_package else module.split(".")[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, *([node.module] if node.module else [])])


def build_graph(src: Path) -> Graph:
    graph = Graph()
    trees: dict[str, tuple[ast.Module, bool]] = {}
    for path in sorted((src / PACKAGE).rglob("*.py")):
        name = module_name(src, path)
        trees[name] = (ast.parse(path.read_text(encoding="utf-8")), path.name == "__init__.py")
        graph.modules.add(name)
        graph.add_def(Symbol(name, ""))
    for name, (tree, _) in trees.items():
        _collect_defs(graph, name, tree)
    for name, (tree, is_package) in trees.items():
        graph.imports[name] = _module_imports(graph, name, is_package, tree)
    for name, (tree, _) in trees.items():
        _collect_edges(graph, name, tree)
    _collect_roots(graph, trees)
    return graph


def _collect_defs(graph: Graph, module: str, tree: ast.Module) -> None:
    def visit(body: list[ast.stmt], prefix: str, owner: str | None) -> None:
        for stmt in body:
            names: list[str] = []
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names = [stmt.name]
            elif isinstance(stmt, ast.Assign):
                names = [t.id for t in stmt.targets if isinstance(t, ast.Name)]
            elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                names = [stmt.target.id]
            for n in names:
                key = graph.add_def(Symbol(module, f"{prefix}{n}"), member=owner is not None)
                if owner is not None:
                    graph.children[owner].add(key)
                if isinstance(stmt, ast.ClassDef):
                    visit(stmt.body, f"{prefix}{n}.", key)

    visit(tree.body, "", None)


def _module_imports(
    graph: Graph, module: str, is_package: bool, tree: ast.Module
) -> dict[str, str]:
    """Local name -> graph key (a module, or `module:name`), for every import at any depth."""
    imports: dict[str, str] = {}
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.ImportFrom):
            source = _resolve_from(module, is_package, stmt)
            if source is None or source.split(".")[0] != PACKAGE:
                continue
            for alias in stmt.names:
                sub = f"{source}.{alias.name}"
                imports[alias.asname or alias.name] = (
                    sub if sub in graph.modules else f"{source}:{alias.name}"
                )
        elif isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name.split(".")[0] == PACKAGE and alias.name in graph.modules:
                    imports[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else PACKAGE
                    )
    return imports


def follow(graph: Graph, target: str) -> str:
    """Follow a re-export: `pkg:Name` where `pkg` only imports `Name` from a submodule."""
    for _ in range(6):
        if target in graph.defs or ":" not in target:
            return target
        module, name = target.split(":")
        nxt = graph.imports.get(module, {}).get(name)
        if nxt is None or nxt == target:
            return target
        target = nxt
    return target


def _collect_edges(graph: Graph, module: str, tree: ast.Module) -> None:
    imports = graph.imports[module]

    def references(owner: str, node: ast.AST) -> None:
        bare, attrs = _names(node)
        edges = graph.edges[owner]
        for sub in ast.walk(node):  # an import runs the module, the name or not
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                for local in (a.asname or a.name.split(".")[0] for a in sub.names):
                    target = imports.get(local, "")
                    edges.add(target.split(":")[0] if target else target)
        for n in bare:
            target = imports.get(n)
            if target is not None:
                edges.add(follow(graph, target))
            if f"{module}:{n}" in graph.defs:
                edges.add(f"{module}:{n}")
        for a in attrs:
            edges.update(graph.members.get(a, ()))  # duck typing: by name
            for n in bare:  # `alias.func` where alias is an imported module
                target = imports.get(n)
                if target in graph.modules and f"{target}:{a}" in graph.defs:
                    edges.add(f"{target}:{a}")

    def visit(body: list[ast.stmt], prefix: str, in_class: bool) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                references(f"{module}:{prefix}{stmt.name}", stmt)
            elif isinstance(stmt, ast.ClassDef):
                key = f"{module}:{prefix}{stmt.name}"
                for part in (*stmt.bases, *stmt.keywords, *stmt.decorator_list):
                    references(key, part)
                visit(stmt.body, f"{prefix}{stmt.name}.", True)
            elif isinstance(stmt, (ast.Assign, ast.AnnAssign)) and stmt.value is not None:
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        references(f"{module}:{prefix}{t.id}", stmt.value)
                if not in_class:
                    references(module, stmt.value)  # evaluated at import
            elif not in_class:
                references(module, stmt)  # module-level statement: runs at import

    visit(tree.body, "", False)


def _collect_roots(graph: Graph, trees: dict[str, tuple[ast.Module, bool]]) -> None:
    for module, (tree, _) in trees.items():
        guarded = any(
            isinstance(s, ast.If)
            and isinstance(s.test, ast.Compare)
            and isinstance(s.test.left, ast.Name)
            and s.test.left.id == "__name__"
            for s in tree.body
        )
        is_cli_group = (
            module.startswith(f"{_CLI_PACKAGE}.")
            and module.count(".") == _CLI_PACKAGE.count(".") + 1
            and not module.rsplit(".", 1)[1].startswith("_")
        )
        if (
            module in _ROOT_MODULES
            or module.startswith(_ROOT_PREFIXES)
            or guarded
            or is_cli_group  # discovered by `registry.discover_groups`
            or module.endswith(".__main__")
        ):
            graph.roots.add(module)
            for key, sym in graph.defs.items():
                if sym.module == module and "." not in sym.qualname and sym.qualname:
                    if module in _ROOT_MODULES or is_cli_group or module.startswith(_ROOT_PREFIXES):
                        graph.roots.add(key)


def reachable(graph: Graph) -> set[str]:
    seen: set[str] = set()
    todo = list(graph.roots)
    while todo:
        key = todo.pop()
        if key in seen:
            continue
        seen.add(key)
        todo.extend(graph.edges.get(key, ()))
        todo.extend(graph.children.get(key, ()))  # members of a reached class
        sym = graph.defs.get(key)
        if sym is not None and sym.qualname:
            todo.append(sym.module)  # a reached name means its module ran
    return seen


# -- what the `req` tests cite ------------------------------------------------------------------


@dataclass(frozen=True)
class Citation:
    test: str  # path::name
    symbol: str  # module:qualname, or ".attr" for a method reached through an object
    kind: str  # "name" | "attr"


def _is_req(node: ast.expr) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    return isinstance(target, ast.Attribute) and target.attr == "req"


def _module_has_req(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            values = (
                stmt.value.elts if isinstance(stmt.value, (ast.List, ast.Tuple)) else [stmt.value]
            )
            if any(_is_req(v) for v in values):
                return True
    return False


def req_tests(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    module_wide = _module_has_req(tree)
    found: list[ast.FunctionDef | ast.AsyncFunctionDef] = []

    def visit(body: list[ast.stmt], inherited: bool) -> None:
        for stmt in body:
            if isinstance(stmt, ast.ClassDef):
                visit(stmt.body, inherited or any(_is_req(d) for d in stmt.decorator_list))
            elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if stmt.name.startswith("test") and (
                    inherited or any(_is_req(d) for d in stmt.decorator_list)
                ):
                    found.append(stmt)

    visit(tree.body, module_wide)
    return found


def _local_imports(tree: ast.Module, module: str | None = None) -> dict[str, str]:
    """Local name -> `shelfhold.x.y[:Name]` for every shelfhold import of the file (any depth)."""
    out: dict[str, str] = {}
    for stmt in ast.walk(tree):
        if isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module:
            if stmt.module.split(".")[0] == PACKAGE:
                for a in stmt.names:
                    out[a.asname or a.name] = f"{stmt.module}:{a.name}"
        elif isinstance(stmt, ast.Import):
            for a in stmt.names:
                if a.name.split(".")[0] == PACKAGE:
                    out[a.asname or a.name.split(".")[0]] = a.name
    return out


def collect_citations(root: Path, graph: Graph) -> list[Citation]:
    tests_dir = root / TESTS
    out: list[Citation] = []
    for path in sorted(tests_dir.rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        tests = req_tests(tree)
        if not tests:
            continue
        imports = _local_imports(tree)
        rel = path.relative_to(root).as_posix()
        for test in tests:
            bare, attrs = _names(test)
            label = f"{rel}::{test.name}"
            for n in sorted(bare & imports.keys()):
                out.append(Citation(label, follow(graph, imports[n]), "name"))
            for a in sorted(attrs):
                out.append(Citation(label, a, "attr"))
    return out


@dataclass(frozen=True)
class Orphan:
    test: str
    symbol: str


def load_allowlist(root: Path) -> list[dict[str, str]]:
    path = root / ALLOWLIST
    if not path.exists():
        return []
    return list(tomllib.loads(path.read_text(encoding="utf-8")).get("allow", []))


def check_allowlist(entries: list[dict[str, str]]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for e in entries:
        sym = e.get("symbol", "?")
        if sym in seen:
            problems.append(f"{sym}: listed twice")
        seen.add(sym)
        if not e.get("reason", "").strip():
            problems.append(f"{sym}: no reason")
        until = e.get("until", "")
        if not UNTIL_RE.match(until):
            problems.append(f"{sym}: until {until!r} names neither an M1b lot nor a milestone")
    return problems


def find_orphans(root: Path) -> list[Orphan]:
    """Cited symbols that no entry point reaches, before the allowlist."""
    graph = build_graph(root / SRC)
    live = reachable(graph)
    orphans: list[Orphan] = []
    for c in collect_citations(root, graph):
        if c.kind == "name":
            sym = graph.defs.get(c.symbol)
            if sym is not None and sym.qualname and c.symbol not in live:
                orphans.append(Orphan(c.test, c.symbol))
        else:  # `x.attr`: orphan when src defines it only as members that none reaches
            members = graph.members.get(c.symbol, set())
            if members and not (members & live):
                orphans.append(Orphan(c.test, f"{sorted(members)[0]}"))
    return sorted(set(orphans), key=lambda o: (o.symbol, o.test))


def _covered(symbol: str, allowed: set[str]) -> bool:
    """An entry covers its symbol and, for a class, its members (`mod:Class.attr`)."""
    while symbol:
        if symbol in allowed:
            return True
        module, _, qual = symbol.partition(":")
        if "." not in qual:
            return False
        symbol = f"{module}:{qual.rsplit('.', 1)[0]}"
    return False


def unlisted_orphans(root: Path) -> list[Orphan]:
    allowed = {e["symbol"] for e in load_allowlist(root)}
    orphans = find_orphans(root)
    names = {o.symbol for o in orphans}
    out = []
    for o in orphans:
        module, _, qual = o.symbol.partition(":")
        owner = f"{module}:{qual.rsplit('.', 1)[0]}" if "." in qual else ""
        if owner in names:
            continue  # reported through its class
        if not _covered(o.symbol, allowed):
            out.append(o)
    return out


_DECLARES = {"add_api_route", "include_router", "add_route", "mount", "add_websocket_route",
             "api_route", "add_middleware"}  # fmt: skip
_DECORATORS = {"get", "post", "put", "delete", "patch", "head", "options", "middleware"}


def route_declarations(root: Path) -> list[str]:
    """`req` tests that declare a route, a middleware, or hand `routers=` (a fake service) to the
    application: the route then exists only in the test, never in `create_app()`."""
    found: list[str] = []
    for path in sorted((root / TESTS).rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for test in req_tests(tree):
            what: set[str] = set()
            for node in ast.walk(test):
                if isinstance(node, ast.Attribute) and node.attr in _DECLARES:
                    what.add(node.attr)
                elif isinstance(node, ast.Name) and node.id == "APIRouter":
                    what.add("APIRouter")
                elif isinstance(node, ast.keyword) and node.arg == "routers":
                    what.add("routers=")
                elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for deco in node.decorator_list:
                        if (
                            isinstance(deco, ast.Call)
                            and isinstance(deco.func, ast.Attribute)
                            and deco.func.attr in _DECORATORS
                        ):
                            what.add(f"@{deco.func.attr}")
            if what:
                rel = path.relative_to(root).as_posix()
                found.append(f"{rel}::{test.name} ({', '.join(sorted(what))})")
    return found


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    bad = unlisted_orphans(root)
    for o in bad:
        print(f"{o.symbol}: cited by {o.test}, reachable from no entry point")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
