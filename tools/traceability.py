#!/usr/bin/env python3
"""Requirement -> test traceability matrix (milestone completion criterion).

Standard library only. Reads the requirement list and milestones from
docs/spec/01-cahier-des-charges.md (EF-/ENF- tables) and the per-milestone
"Exigences couvertes" lists of docs/spec/03-roadmap-tdd.md, collects the `req`
markers of the tests, writes docs/traceability/<milestone>.md and exits 1 when,
for the requested milestone, a requirement has no test, a test cites an unknown
ID, or a test has no marker and is not listed in tools/traceability-exceptions.toml.

Marker conventions
- pytest:       @pytest.mark.req("ENF-OPS-002", ...)  (function, class, or `pytestmark`)
- Vitest/Playwright: a `// req: ID, ID` comment right above `test(`/`it(`, or a
  `// req-file: ID` comment that applies to every test of the file.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ID_RE = re.compile(r"\b(?:EF|ENF)-[A-Z0-9]+-\d{3}\b")
MILESTONE_RE = re.compile(r"\bM(\d)([ab])?\b")
CDC = "docs/spec/01-cahier-des-charges.md"
ROADMAP = "docs/spec/03-roadmap-tdd.md"
EXCEPTIONS = "tools/traceability-exceptions.toml"
SKIP_DIRS = {"node_modules", ".venv", "__pycache__", ".svelte-kit", "build", "dist"}
_CELL_SPLIT = re.compile(r"(?<!\\)\|")


@dataclass
class TestRef:
    __test__ = False  # not a pytest class

    path: str
    name: str
    reqs: list[str] = field(default_factory=list)
    # Why the test may not run in the required CI: contract, skip, skipif, xfail...
    conditional: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.path}::{self.name}"


def _ms_key(ms: str) -> tuple[int, str]:
    m = MILESTONE_RE.fullmatch(ms)
    assert m
    return int(m.group(1)), m.group(2) or ""


def parse_requirements(text: str) -> dict[str, list[str]]:
    """ID -> milestones (in order of appearance) from the EF-/ENF- tables."""
    reqs: dict[str, list[str]] = {}
    for line in text.splitlines():
        if not re.match(r"\|\s*(?:EF|ENF)-[A-Z0-9]+-\d{3}\s*\|", line):
            continue
        cells = [c.strip() for c in _CELL_SPLIT.split(line.strip().strip("|"))]
        if len(cells) < 3:
            continue
        # The milestone column is always the second to last (before "Origine").
        reqs[cells[0]] = [m.group(0) for m in MILESTONE_RE.finditer(cells[-2])]
    return reqs


def earliest(milestones: list[str]) -> str | None:
    return min(milestones, key=_ms_key) if milestones else None


def parse_roadmap_coverage(text: str) -> dict[str, set[str]]:
    """Milestone -> IDs listed under its "Exigences couvertes" block."""
    cov: dict[str, set[str]] = {}
    current: str | None = None
    in_block = False
    for line in text.splitlines():
        heading = re.match(r"###\s+(M\d[ab]?)\b", line)
        if heading:
            current, in_block = heading.group(1), False
            continue
        if line.startswith("#"):
            current, in_block = None, False
            continue
        if current is None:
            continue
        if line.startswith("**Exigences couvertes."):
            in_block = True
            cov.setdefault(current, set())
        elif in_block and (line.startswith("**") or line.strip() == "---"):
            in_block = False
        if in_block and not line.startswith("- Décompte"):
            cov[current].update(ID_RE.findall(line))
    return cov


def compare_specs(
    reqs: dict[str, list[str]], cov: dict[str, set[str]], ms: str
) -> list[str]:
    issues: list[str] = []
    in_01 = {i for i, m in reqs.items() if earliest(m) == ms}
    in_03 = cov.get(ms, set())
    for i in sorted(in_01 - in_03):
        issues.append(
            f"{i}: jalon {ms} dans 01 mais absente de la liste « Exigences couvertes » de 03"
        )
    for i in sorted(in_03):
        if i not in reqs:
            issues.append(f"{i}: listée par 03 pour {ms} mais inconnue de 01")
        elif i not in in_01:
            issues.append(
                f"{i}: listée par 03 pour {ms}, mais 01 donne {' / '.join(reqs[i]) or 'aucun jalon'}"
            )
    return issues


def _iter_files(root: Path, base: str, patterns: tuple[str, ...]) -> list[Path]:
    start = root / base
    if not start.is_dir():
        return []
    out: set[Path] = set()
    for pat in patterns:
        for p in start.rglob(pat):
            if p.is_file() and not SKIP_DIRS.intersection(p.relative_to(start).parts):
                out.add(p)
    return sorted(out)


def _req_args(node: ast.expr) -> list[str] | None:
    """IDs of a `pytest.mark.req(...)` expression, else None."""
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "req"
        and isinstance(node.func.value, ast.Attribute)
        and node.func.value.attr == "mark"
    ):
        return [
            a.value
            for a in node.args
            if isinstance(a, ast.Constant) and isinstance(a.value, str)
        ]
    return None


CONDITIONAL_MARKS = {"contract", "skip", "skipif", "xfail"}


def _mark_name(node: ast.expr) -> str | None:
    """`pytest.mark.<name>` or `pytest.mark.<name>(...)` -> name."""
    if isinstance(node, ast.Call):
        node = node.func
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "mark"
    ):
        return node.attr
    return None


def _decorator_reqs(decorators: list[ast.expr]) -> list[str]:
    out: list[str] = []
    for d in decorators:
        out += _req_args(d) or []
    return out


def _decorator_flags(decorators: list[ast.expr]) -> list[str]:
    return [n for d in decorators if (n := _mark_name(d)) in CONDITIONAL_MARKS]


def collect_python(root: Path) -> list[TestRef]:
    refs: list[TestRef] = []
    for f in _iter_files(root, "backend/tests", ("test_*.py",)) + _iter_files(
        root, "e2e", ("test_*.py",)
    ):
        rel = f.relative_to(root).as_posix()
        tree = ast.parse(f.read_text(encoding="utf-8"))
        module: list[str] = []
        module_flags: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
            ):
                items = (
                    node.value.elts
                    if isinstance(node.value, ast.List | ast.Tuple)
                    else [node.value]
                )
                for item in items:
                    module += _req_args(item) or []
                    module_flags += _decorator_flags([item])
        for node in tree.body:
            if isinstance(
                node, ast.FunctionDef | ast.AsyncFunctionDef
            ) and node.name.startswith("test"):
                refs.append(
                    TestRef(
                        rel,
                        node.name,
                        _decorator_reqs(node.decorator_list) + module,
                        _decorator_flags(node.decorator_list) + module_flags,
                    )
                )
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                cls = _decorator_reqs(node.decorator_list)
                cls_flags = _decorator_flags(node.decorator_list)
                for sub in node.body:
                    if isinstance(
                        sub, ast.FunctionDef | ast.AsyncFunctionDef
                    ) and sub.name.startswith("test"):
                        reqs = _decorator_reqs(sub.decorator_list) + cls + module
                        flags = (
                            _decorator_flags(sub.decorator_list)
                            + cls_flags
                            + module_flags
                        )
                        refs.append(
                            TestRef(rel, f"{node.name}::{sub.name}", reqs, flags)
                        )
    return refs


_TS_TEST = re.compile(r"^\s*(?:test|it)((?:\.\w+)*)\(\s*(['\"`])(.+?)\2")
_TS_REQ = re.compile(r"^\s*//\s*req:\s*(.*)$")
_TS_REQ_FILE = re.compile(r"^\s*//\s*req-file:\s*(.*)$")


def collect_typescript(root: Path) -> list[TestRef]:
    refs: list[TestRef] = []
    for base in ("frontend", "e2e"):
        for f in _iter_files(root, base, ("*.test.ts", "*.spec.ts")):
            rel = f.relative_to(root).as_posix()
            lines = f.read_text(encoding="utf-8").splitlines()
            file_level: list[str] = []
            for line in lines:
                m = _TS_REQ_FILE.match(line)
                if m:
                    file_level += ID_RE.findall(m.group(1))
            pending: list[str] = []
            for line in lines:
                m = _TS_REQ.match(line)
                if m:
                    pending += ID_RE.findall(m.group(1))
                    continue
                t = _TS_TEST.match(line)
                if t:
                    mods = [m for m in t.group(1).split(".") if m]
                    flags = [m for m in mods if m in ("skip", "fixme", "todo")]
                    refs.append(TestRef(rel, t.group(3), pending + file_level, flags))
                if line.strip() and not line.lstrip().startswith("//"):
                    pending = []
    return refs


def _load_table(root: Path, table: str, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    path = root / EXCEPTIONS
    if not path.is_file():
        return []
    items = tomllib.loads(path.read_text(encoding="utf-8")).get(table, [])
    for item in items:
        if any(not str(item.get(k, "")).strip() for k in (*keys, "reason")):
            raise SystemExit(
                f"{EXCEPTIONS}: every [[{table}]] needs {', '.join(keys)} and a justified `reason`"
            )
    return list(items)


def load_exceptions(root: Path) -> list[dict[str, Any]]:
    return _load_table(root, "exception", ("path",))


def load_out_of_required_ci(root: Path) -> list[dict[str, Any]]:
    return _load_table(root, "out_of_required_ci", ("path", "workflow"))


def load_deferred(root: Path) -> list[dict[str, Any]]:
    return _load_table(root, "deferred", ("requirement", "aspect"))


def _excepted(ref: TestRef, exceptions: list[dict[str, Any]]) -> bool:
    for e in exceptions:
        if e["path"] != ref.path:
            continue
        names = e.get("tests")
        if names is None and "test" in e:
            names = [e["test"]]
        if names is None or ref.name in names:
            return True
    return False


def render(
    ms: str,
    required: list[str],
    reqs: dict[str, list[str]],
    by_req: dict[str, list[str]],
    cond_req: dict[str, list[str]],
    issues: list[str],
    exceptions: list[dict[str, Any]],
    deferred: list[dict[str, Any]],
) -> str:
    covered = [r for r in required if by_req.get(r) or cond_req.get(r)]
    out = [
        f"# Matrice de traçabilité — {ms}",
        "",
        "Générée par `tools/traceability.py` (ne pas éditer à la main). "
        f"Exigences du jalon {ms} : **{len(required)}**, couvertes : **{len(covered)}**, "
        f"non couvertes : **{len(required) - len(covered)}**.",
        "",
        "| Exigence | Jalons (01) | Statut | Tests (CI requise) | Hors CI requise / conditionnel |",
        "|---|---|---|---|---|",
    ]
    for r in required:
        tests = by_req.get(r, [])
        cond = cond_req.get(r, [])
        if tests:
            status = "couverte"
        elif cond:
            status = "COUVERTE (hors CI requise)"
        else:
            status = "NON COUVERTE"
        cell = "<br>".join(f"`{t}`" for t in tests) if tests else "—"
        cond_cell = "<br>".join(f"`{t}`" for t in cond) if cond else "—"
        out.append(
            f"| {r} | {' / '.join(reqs.get(r, [])) or '—'} | {status} | {cell} | {cond_cell} |"
        )
    out += ["", "## Écarts entre 01 (cahier des charges) et 03 (feuille de route)", ""]
    out += [f"- {i}" for i in issues] or ["Aucun."]
    out += ["", "## Tests exemptés du marqueur `req`", ""]
    out += [
        f"- `{e['path']}`{(' (' + ', '.join(e.get('tests') or [e['test']]) + ')') if (e.get('tests') or e.get('test')) else ''} : {e['reason']}"
        for e in exceptions
    ] or ["Aucun."]
    out += ["", "## Volets reportés (exigences à plusieurs volets)", ""]
    out += [
        f"- {d['requirement']} : {d['aspect']} — {d['reason']}" for d in deferred
    ] or ["Aucun."]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--milestone", required=True, help="e.g. M0")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[1]))
    ap.add_argument("--output", help="default: docs/traceability/<milestone>.md")
    args = ap.parse_args(argv)
    ms = args.milestone
    if not MILESTONE_RE.fullmatch(ms):
        raise SystemExit(f"invalid milestone: {ms}")
    root = Path(args.root)
    reqs = parse_requirements((root / CDC).read_text(encoding="utf-8"))
    cov = parse_roadmap_coverage((root / ROADMAP).read_text(encoding="utf-8"))
    issues = compare_specs(reqs, cov, ms)
    required = sorted(
        {i for i, m in reqs.items() if earliest(m) == ms}
        | {i for i in cov.get(ms, set()) if i in reqs}
    )
    exceptions = load_exceptions(root)
    tests = collect_python(root) + collect_typescript(root)

    errors: list[str] = []
    by_req: dict[str, list[str]] = {}
    cond_req: dict[str, list[str]] = {}
    ci_exc = {e["path"]: e for e in load_out_of_required_ci(root)}
    for t in tests:
        for r in t.reqs:
            if r not in reqs:
                errors.append(f"{t.label}: ID inconnu {r}")
            elif t.conditional:
                wf = ci_exc.get(t.path, {}).get("workflow", "?")
                cond_req.setdefault(r, []).append(
                    f"{t.label} [{'/'.join(t.conditional)} ; {wf}]"
                )
            else:
                by_req.setdefault(r, []).append(t.label)
        if not t.reqs and not _excepted(t, exceptions):
            errors.append(
                f"{t.label}: test orphelin (aucun marqueur req, hors exceptions)"
            )
    for r in required:
        if by_req.get(r):
            continue
        if not cond_req.get(r):
            errors.append(f"{r}: exigence {ms} sans aucun test")
        elif any("[" in c and c.endswith("; ?]") for c in cond_req[r]):
            errors.append(
                f"{r}: couverte seulement hors CI requise, workflow non justifié dans {EXCEPTIONS}"
            )

    matrix = render(
        ms, required, reqs, by_req, cond_req, issues, exceptions, load_deferred(root)
    )
    out = (
        Path(args.output)
        if args.output
        else root / "docs" / "traceability" / f"{ms}.md"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(matrix, encoding="utf-8")
    for i in issues:
        print(f"warning: {i}", file=sys.stderr)
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    covered = sum(1 for r in required if by_req.get(r) or cond_req.get(r))
    print(
        f"{ms}: {covered}/{len(required)} exigences couvertes, {len(errors)} erreur(s)"
    )
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
