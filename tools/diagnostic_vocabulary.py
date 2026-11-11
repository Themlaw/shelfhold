#!/usr/bin/env python3
"""The closed vocabulary of the diagnostic bundle, read from the code (M2 L30, ENF-LEG-009).

A log line enters the bundle only through the words the code itself writes: an event name is a
literal of a `log.<level>("…")` call (or of `log_outcome("…")`); a field is the keyword of such
a call or a bound context variable; the value of a word field (`outcome`, `code`, `reason`…) is
a literal the code gives that name (`outcome="ok"`, `{"code": "…"}`, `code = "…"`), or a value of
one of its closed sets (an `Enum`, a `Literal[...]`, an upper-case tuple of words); an `error` is
an exception class the code defines, catches or raises, or a builtin one; a `logger` is a module
of the `shelfhold` package. Anything else becomes `<other>` (or is dropped, for a field).

    python3 tools/diagnostic_vocabulary.py          # rewrites the JSON file
    python3 tools/diagnostic_vocabulary.py --check  # exit 1 when the file is not up to date

The file is checked in so that a widening shows in review: `test_diagnostic_vocabulary` fails
until it is regenerated.
"""

from __future__ import annotations

import argparse
import ast
import builtins
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "backend" / "src" / "shelfhold"
OUTPUT = SOURCE / "ops" / "diagnostic_vocabulary.json"

LEVELS = frozenset({"debug", "info", "warning", "error", "critical", "exception"})
# The fields whose text value is a word of the code (the bundle keeps it when it is one).
WORD_KEYS = (
    "action", "capability", "code", "decision", "format", "job_kind", "kind", "level", "media",
    "mode", "outcome", "phase", "queue", "reason", "result", "retention", "state", "status",
    "strategy", "transport",
)  # fmt: skip
BIND_CALLS = frozenset({"bind_contextvars", "bound_contextvars", "bind"})
WORD = re.compile(r"^[a-z][a-z0-9_.:-]{0,63}$")
EVENT = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z0-9_]+)*$")
KEY = re.compile(r"^[a-z][a-z0-9_]{0,48}$")
CLASS = re.compile(r"^[A-Z][A-Za-z0-9_]{0,63}$")
MODULE_PART = re.compile(r"^[a-z_][a-z0-9_]*$")
# A header that names a credential or an identity (`x-shelfhold-proxy-secret`, `x-webauth-user`)
# is no word of a log line: it becomes `<other>`, and the vocabulary file never looks like a key
# to a secret scanner.
CREDENTIAL_HEADER = re.compile(
    r"^x-.*(?:secret|token|key|auth|pass|session|cookie|credential)", re.IGNORECASE
)
EXCEPTION_SUFFIXES = ("Error", "Exception", "Refused", "Exit", "Interrupt", "Timeout")


def _modules(root: Path) -> Iterator[tuple[str, Path]]:
    for path in sorted(root.rglob("*.py")):
        parts = list(path.relative_to(root.parent).with_suffix("").parts)
        if parts[-1] == "__init__":
            parts = parts[:-1]
        if all(MODULE_PART.match(p) for p in parts):
            yield ".".join(parts), path


def _str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _strings(node: ast.AST) -> Iterator[str]:
    """The string literals of a tuple, list, set, frozenset(...) or Literal[...]."""
    if isinstance(node, ast.Call) and node.args:
        node = node.args[0]
    if isinstance(node, ast.Subscript):
        node = node.slice
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        for item in node.elts:
            value = _str(item)
            if value is not None:
                yield value
    else:
        value = _str(node)
        if value is not None:
            yield value


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


class _Collector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.events: set[str] = set()
        self.keys: set[str] = set()
        self.words: dict[str, set[str]] = {key: set() for key in WORD_KEYS}
        self.shared: set[str] = set()
        self.errors: set[str] = set()
        self.classes: dict[str, set[str]] = {}

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in LEVELS and node.args:
            event = _str(node.args[0])
            if event is not None and EVENT.match(event):
                self.events.add(event)
                self.keys.update(k.arg for k in node.keywords if k.arg and KEY.match(k.arg))
        name = _name_of(func)
        if name == "log_outcome" and node.args:
            event = _str(node.args[0])
            if event is not None and EVENT.match(event):
                self.events.add(event)
        if name in BIND_CALLS:
            self.keys.update(k.arg for k in node.keywords if k.arg and KEY.match(k.arg))
        for keyword in node.keywords:
            if keyword.arg in self.words:
                self.words[keyword.arg].update(_strings(keyword.value))
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            name = _str(key) if key is not None else None
            if name in self.words:
                self.words[name].update(_strings(value))
        self.generic_visit(node)

    def _assigned(self, target: ast.AST, value: ast.AST | None) -> None:
        if value is None:
            return
        name = _name_of(target)
        if name in self.words:
            self.words[name].update(_strings(value))
        elif name is not None and name.isupper():
            self.shared.update(_strings(value))

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._assigned(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._assigned(node.target, node.value)
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if _name_of(node.value) == "Literal":
            self.shared.update(_strings(node))
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        bases = {b for b in (_name_of(base) for base in node.bases) if b}
        self.classes[node.name] = bases
        if bases & {"Enum", "StrEnum", "IntEnum"}:
            for statement in node.body:
                if isinstance(statement, ast.Assign):
                    self.shared.update(_strings(statement.value))
        self.generic_visit(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.type is not None:
            types = node.type.elts if isinstance(node.type, ast.Tuple) else [node.type]
            self.errors.update(n for n in (_name_of(t) for t in types) if n)
        self.generic_visit(node)

    def visit_Raise(self, node: ast.Raise) -> None:
        raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = _name_of(raised) if raised is not None else None
        if name:
            self.errors.add(name)
        self.generic_visit(node)


def _exception_classes(classes: dict[str, set[str]], known: set[str]) -> set[str]:
    """The classes of the code that derive (by name, transitively) from an exception."""
    found = set(known)
    changed = True
    while changed:
        changed = False
        for name, bases in classes.items():
            if name not in found and (bases & found or name.endswith(EXCEPTION_SUFFIXES)):
                found.add(name)
                changed = True
    return {name for name in found if name in classes}


def _word(value: str) -> bool:
    return bool(WORD.match(value)) and not CREDENTIAL_HEADER.match(value)


def extract(source: Path = SOURCE) -> dict[str, object]:
    collector = _Collector()
    loggers: set[str] = set()
    for module, path in _modules(source):
        loggers.add(module)
        collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    builtin_errors = {
        name
        for name, value in vars(builtins).items()
        if isinstance(value, type) and issubclass(value, BaseException)
    }
    errors = (
        {e for e in collector.errors if CLASS.match(e)}
        | builtin_errors
        | _exception_classes(collector.classes, builtin_errors | collector.errors)
    )
    shared = {w for w in collector.shared if _word(w)}
    words = {
        key: sorted(w for w in values if _word(w) and w not in shared)
        for key, values in sorted(collector.words.items())
    }
    words["level"] = sorted(set(words["level"]) | (LEVELS - {"exception"}) - shared)
    return {
        "comment": "generated by tools/diagnostic_vocabulary.py; do not edit by hand",
        "events": sorted(collector.events),
        "keys": sorted(collector.keys),
        "words": words,
        "shared_words": sorted(shared),
        "errors": sorted(e for e in errors if CLASS.match(e)),
        "loggers": sorted(loggers),
    }


def render(vocabulary: dict[str, object]) -> str:
    return json.dumps(vocabulary, ensure_ascii=False, indent=1, sort_keys=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    text = render(extract())
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(f"{OUTPUT.relative_to(REPO)} is out of date: run tools/diagnostic_vocabulary.py")
            return 1
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
