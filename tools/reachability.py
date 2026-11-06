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
import fnmatch
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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
        self.fields: set[str] = set()  # members that are data (dataclass fields, constants)
        self.implicit: set[str] = set()  # members reached with their class (fields, dunders, hooks)
        self.owner: dict[str, str] = {}  # member key -> class key
        self.attrs: dict[str, set[str]] = defaultdict(set)  # def key -> attribute names it uses
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


_PLAIN_DECORATORS = {
    "staticmethod", "classmethod", "property", "abstractmethod", "override", "contextmanager",
    "asynccontextmanager", "cache", "cached_property", "lru_cache", "overload", "final", "setter",
}  # fmt: skip


def _is_hook(stmt: ast.stmt) -> bool:
    """A method reached with its class: a dunder, or one a framework calls (any other decorator:
    validators, computed fields, route or signal registration)."""
    assert isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
    if stmt.name.startswith("__") and stmt.name.endswith("__"):
        return True
    for deco in stmt.decorator_list:
        target = deco.func if isinstance(deco, ast.Call) else deco
        name = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", "")
        if name not in _PLAIN_DECORATORS:
            return True
    return False


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
                    graph.owner[key] = owner
                    is_func = isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef))
                    if not is_func:
                        graph.fields.add(key)
                    if not is_func or _is_hook(stmt):
                        graph.implicit.add(key)
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
        for sub in ast.walk(node):  # `getattr(service, "sub_sources", None)`: a use by name
            if (
                isinstance(sub, ast.Call)
                and isinstance(sub.func, ast.Name)
                and sub.func.id in {"getattr", "hasattr"}
                and len(sub.args) >= 2
                and isinstance(sub.args[1], ast.Constant)
                and isinstance(sub.args[1].value, str)
            ):
                attrs = {*attrs, sub.args[1].value}
        graph.attrs[owner].update(attrs)  # duck typing: by name, once the class is reached
        for a in attrs:
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
    """Walk from the roots. A method is reached when its class is reached (built or named) AND
    some reached code uses an attribute of that name; fields, dunders and framework hooks come
    with their class."""
    seen: set[str] = set()
    live_attrs: set[str] = set()
    todo = list(graph.roots)
    while todo:
        key = todo.pop()
        if key in seen:
            continue
        seen.add(key)
        todo.extend(graph.edges.get(key, ()))
        for name in graph.attrs.get(key, set()) - live_attrs:
            live_attrs.add(name)
            todo.extend(m for m in graph.members.get(name, ()) if graph.owner.get(m) in seen)
        for child in graph.children.get(key, ()):  # members of a class just reached
            if child in graph.implicit or graph.defs[child].simple in live_attrs:
                todo.append(child)
        owner = graph.owner.get(key)
        if owner is not None:
            todo.append(owner)  # a reached method names its class
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


def _helper_imports(root: Path, tree: ast.Module, seen: set[str]) -> list[tuple[str, ast.Module]]:
    """The test-support modules (`tests/*_env.py`, fixtures, fakes) a test file imports,
    transitively: what they build is what the test exercises."""
    out: list[tuple[str, ast.Module]] = []
    for stmt in ast.walk(tree):
        names: list[str] = []
        if isinstance(stmt, ast.ImportFrom) and stmt.level == 0 and stmt.module:
            names = [stmt.module, *(f"{stmt.module}.{a.name}" for a in stmt.names)]
        elif isinstance(stmt, ast.Import):
            names = [a.name for a in stmt.names]
        for name in names:
            parts = name.split(".")
            if parts[0] != "tests":
                continue
            candidate = root / TESTS.rsplit("/", 1)[0] / Path(*parts).with_suffix(".py")
            if not candidate.is_file() or candidate.name.startswith("test_"):
                continue
            key = candidate.as_posix()
            if key in seen:
                continue
            seen.add(key)
            sub = ast.parse(candidate.read_text(encoding="utf-8"))
            out.append((candidate.relative_to(root).as_posix(), sub))
            out.extend(_helper_imports(root, sub, seen))
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
        for helper, sub in _helper_imports(root, tree, set()):  # what the test builds through them
            used, _ = _names(sub)
            for n, target in sorted(_local_imports(sub).items()):
                if n in used:
                    out.append(Citation(f"{rel} (via {helper})", follow(graph, target), "name"))
    return out


@dataclass(frozen=True)
class Orphan:
    test: str
    symbol: str


PERMANENT_CATEGORIES = ("public-sdk", "governance-catalogue", "generic-mechanics")


def load_allowlist(root: Path) -> list[dict[str, Any]]:
    """Entries: `symbols` (a list; `pkg.mod.*` matches a whole module prefix), `category`,
    `reason`, `until`."""
    path = root / ALLOWLIST
    if not path.exists():
        return []
    return list(tomllib.loads(path.read_text(encoding="utf-8")).get("allow", []))


def check_allowlist(entries: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    seen: set[str] = set()
    for e in entries:
        label = e.get("category", "?")
        symbols = e.get("symbols", e.get("tests", []))
        if not symbols:
            problems.append(f"{label}: no symbols or tests")
        for sym in symbols:
            if sym in seen:
                problems.append(f"{sym}: listed twice")
            seen.add(sym)
        if not str(e.get("reason", "")).strip():
            problems.append(f"{label}: no reason")
        until = e.get("until", "")
        permanent = until == "permanent" and label in PERMANENT_CATEGORIES
        if not permanent and not UNTIL_RE.match(until):
            problems.append(f"{label}: until {until!r} names neither an M1b lot nor a milestone")
    return problems


def find_orphans(root: Path) -> list[Orphan]:
    """Cited symbols that no entry point reaches, before the allowlist."""
    graph = build_graph(root / SRC)
    live = reachable(graph)
    citations = collect_citations(root, graph)
    classes: dict[str, set[str]] = defaultdict(set)  # test -> classes it names
    for c in citations:
        if c.kind == "name" and c.symbol in graph.children:
            classes[c.test.split(" ")[0].split("::")[0]].add(c.symbol)
    orphans: list[Orphan] = []
    for c in citations:
        if c.kind == "name":
            sym = graph.defs.get(c.symbol)
            if sym is not None and sym.qualname and c.symbol not in live:
                orphans.append(Orphan(c.test, c.symbol))
            continue
        members = graph.members.get(c.symbol, set()) - graph.fields  # a field follows its class
        named = classes[c.test.split(" ")[0].split("::")[0]]
        mine = {m for m in members if graph.owner.get(m) in named}
        if mine:  # the test names the class: a same-named method elsewhere proves nothing
            orphans.extend(Orphan(c.test, m) for m in sorted(mine - live))
        elif members and not (members & live):
            orphans.append(Orphan(c.test, sorted(members)[0]))
    return sorted(set(orphans), key=lambda o: (o.symbol, o.test))


def _matches(symbol: str, pattern: str) -> bool:
    return fnmatch.fnmatchcase(symbol, pattern if "*" in pattern else pattern)


def _covered(symbol: str, patterns: set[str]) -> bool:
    """An entry covers its symbol, the members of a class (`mod:Class.attr`), and, with `*`, a
    module prefix (`shelfhold.sdk.*`)."""
    while symbol:
        if any(_matches(symbol, p) for p in patterns):
            return True
        module, _, qual = symbol.partition(":")
        if "." not in qual:
            return False
        symbol = f"{module}:{qual.rsplit('.', 1)[0]}"
    return False


def allowed_patterns(root: Path) -> set[str]:
    return {sym for e in load_allowlist(root) for sym in e.get("symbols", [])}


def unlisted_orphans(root: Path) -> list[Orphan]:
    allowed = allowed_patterns(root)
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


def load_route_tests(root: Path) -> list[dict[str, Any]]:
    """`[[route_test]]` entries: tests that may declare a route, each with a category, a reason
    and an `until`."""
    path = root / ALLOWLIST
    if not path.exists():
        return []
    return list(tomllib.loads(path.read_text(encoding="utf-8")).get("route_test", []))


def _declares(node: ast.AST) -> set[str]:
    """What under `node` declares a route, a middleware, a router, or hands `routers=` over."""
    what: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in _DECLARES:
            what.add(sub.attr)
        elif (
            isinstance(sub, ast.Call)
            and isinstance(sub.func, ast.Name)
            and sub.func.id == "APIRouter"
        ):
            what.add("APIRouter")
        elif (
            isinstance(sub, ast.keyword)
            and sub.arg == "routers"
            and not isinstance(sub.value, ast.Name)  # `routers=routers`: a pass-through parameter
        ):
            what.add("routers=")
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for deco in sub.decorator_list:
                if (
                    isinstance(deco, ast.Call)
                    and isinstance(deco.func, ast.Attribute)
                    and deco.func.attr in _DECORATORS
                ):
                    what.add(f"@{deco.func.attr}")
    return what


def _module_functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    return {s.name: s for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))}


def route_declarations(root: Path, *, listed: bool = False) -> list[str]:
    """`req` tests that declare a route, a middleware, or hand `routers=` (a fake service) to the
    application, in their body, in a helper or fixture of their module that they use, or in a
    test-support module (`tests/*_env.py`) their file imports: the route then exists only in the
    test, never in `create_app()`. With `listed`, the ones the allowlist excuses are reported."""
    excused = {t for e in load_route_tests(root) for t in e.get("tests", [])}
    found: list[str] = []
    for path in sorted((root / TESTS).rglob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        tests = req_tests(tree)
        if not tests:
            continue
        rel = path.relative_to(root).as_posix()
        helpers = _module_functions(tree)
        support: dict[str, set[str]] = {}
        for helper_path, sub in _helper_imports(root, tree, set()):
            what = _declares(sub)
            if what:
                support[helper_path] = what
        for test in tests:
            what = _declares(test)
            used, _ = _names(test)
            args = {a.arg for a in test.args.args}
            for name in sorted((used | args) & helpers.keys() - {test.name}):
                what |= _declares(helpers[name])  # a helper or a fixture of the module
            via = [f"{h}: {', '.join(sorted(w))}" for h, w in sorted(support.items())]
            if what or via:
                label = f"{rel}::{test.name}"
                if listed or label not in excused:
                    detail = ", ".join(sorted(what)) or "; ".join(via)
                    found.append(f"{label} ({detail})")
    return found


def main(argv: list[str]) -> int:
    root = Path(argv[1]) if len(argv) > 1 else Path(__file__).resolve().parents[1]
    bad = unlisted_orphans(root)
    for o in bad:
        print(f"{o.symbol}: cited by {o.test}, reachable from no entry point")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
