#!/usr/bin/env python3
"""foliate-js is vendored as a submodule pinned by commit (EF-LEC-003, M5 L11, risk R13).

Standard library only; nothing is fetched and nothing from the submodule is executed.

`pin`: the gitlink recorded by Git (`git ls-files -s`, so no checkout of the submodule is
needed) is the commit written in `docs/dependencies.md` ("Commit épinglé"), and `.gitmodules`
names the official repository over TLS, with no `branch` and no command (`update = !…`).

`assets`: the reader loads nothing from a third-party origin. It scans the application sources
(`frontend/src`, `frontend/scripts`, HTML entries) for any way to load an absolute URL, then the
vendored code (comments removed) for every absolute URL and every network primitive. The only
hosts admitted in the vendored code are XML namespaces and spec identifiers, which name things and
are never fetched; a new host or a new loader fails here, so an update goes to manual review.
`--require-checkout` fails when the submodule is not checked out (the CI job that builds the
frontend must have it).

Usage: check_submodule_pin.py pin | assets [--require-checkout]
"""

from __future__ import annotations

import argparse
import configparser
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
SUBMODULE = "frontend/vendor/foliate-js"
OFFICIAL_URL = "https://github.com/johnfactotum/foliate-js.git"
DEPENDENCIES = "docs/dependencies.md"
PINNED = re.compile(r"Commit épinglé\s*:\s*`([0-9A-Za-z]+)`")
FULL_SHA = re.compile(r"[0-9a-f]{40}")

# XML namespaces and specification identifiers that the vendored code names but never fetches.
NAMESPACE_HOSTS = frozenset(
    {
        "www.w3.org",
        "www.idpf.org",
        "purl.org",
        "opds-spec.org",
        "www.daisy.org",
        "id.loc.gov",
        "schema.org",
        "www.editeur.org",
        "www.prismstandard.org",
        "ns.adobe.com",
        "invalid.invalid",  # base of a relative-URL resolution, never requested
    }
)
# The subtrees of the submodule that are not part of the library: tests, CI, build scripts.
NOT_RUNTIME = ("tests/", ".github/", "rollup/", "rollup.config.js", "eslint.config.js")

NETWORK_PRIMITIVES = re.compile(
    r"\b(importScripts|XMLHttpRequest|WebSocket|EventSource|sendBeacon)\b|new\s+(Shared)?Worker\s*\("
)
URL_LITERAL = re.compile(r"(?<![\w.-])https?://([A-Za-z0-9._-]+)")
# How an application source loads something from an absolute (or protocol-relative) URL.
APP_LOADERS = (
    re.compile(r"""\b(?:src|href|action|poster|data|srcset)\s*=\s*["'`]\s*(?:https?:)?//""", re.I),
    re.compile(r"""url\(\s*["']?\s*(?:https?:)?//""", re.I),
    re.compile(r"""@import\s+(?:url\(\s*)?["']?\s*(?:https?:)?//""", re.I),
    re.compile(r"""\bfrom\s+["'`]\s*(?:https?:)?//"""),
    re.compile(r"""\bimport\s*\(\s*["'`]\s*(?:https?:)?//"""),
    re.compile(
        r"""\b(?:fetch|importScripts|WebSocket|EventSource)\s*\(\s*["'`]\s*(?:https?:)?//"""
    ),
    re.compile(r"""new\s+(?:Shared)?Worker\s*\(\s*["'`]\s*(?:https?:)?//"""),
    re.compile(r"""\bsendBeacon\s*\(\s*["'`]\s*(?:https?:)?//"""),
)
APP_DIRS = ("frontend/src", "frontend/scripts", "frontend/static")
APP_FILES = ("frontend/reader.html", "frontend/app.html", "frontend/index.html")
APP_SUFFIXES = {".ts", ".js", ".mjs", ".svelte", ".html", ".css", ".json", ".webmanifest"}


def _git(root: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), *args],  # noqa: S607
        check=False,
        capture_output=True,
        text=True,
    )
    return done.stdout if done.returncode == 0 else ""


def gitlink_commit(root: Path = ROOT) -> str | None:
    """The commit Git records for the submodule path (mode 160000), or None."""
    for line in _git(root, "ls-files", "-s", "--", SUBMODULE).splitlines():
        mode, sha, _stage, _path = line.replace("\t", " ").split(" ", 3)
        if mode == "160000":
            return sha
    return None


def documented_commit(root: Path = ROOT) -> str | None:
    path = root / DEPENDENCIES
    if not path.exists():
        return None
    found = PINNED.search(path.read_text(encoding="utf-8"))
    return found.group(1) if found else None


def pin_problems(root: Path = ROOT) -> list[str]:
    problems: list[str] = []
    linked = gitlink_commit(root)
    documented = documented_commit(root)
    if linked is None:
        problems.append(f"{SUBMODULE} is not a gitlink (submodule missing)")
    if documented is None:
        problems.append(f"{DEPENDENCIES}: no line 'Commit épinglé : `<sha>`'")
    elif not FULL_SHA.fullmatch(documented):
        problems.append(f"{DEPENDENCIES}: the pinned commit must be 40 lowercase hex digits")
    if linked and documented and linked != documented:
        problems.append(
            f"{DEPENDENCIES} documents {documented} but the gitlink is {linked}: "
            "an update is a reviewed change of both, in the same commit"
        )
    problems.extend(_gitmodules_problems(root))
    return problems


def _gitmodules_problems(root: Path) -> list[str]:
    path = root / ".gitmodules"
    if not path.exists():
        return [".gitmodules missing"]
    parser = configparser.ConfigParser(strict=False)
    parser.read(path, encoding="utf-8")
    sections = parser.sections()
    if sections != [f'submodule "{SUBMODULE}"']:
        return [f".gitmodules must declare only {SUBMODULE}, found {sections}"]
    section = parser[sections[0]]
    problems: list[str] = []
    if section.get("url") != OFFICIAL_URL:
        problems.append(f".gitmodules url must be {OFFICIAL_URL}, found {section.get('url')}")
    for key in section:
        if key not in {"path", "url"}:
            problems.append(f".gitmodules: '{key}' is not allowed (branch and update are refused)")
    return problems


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(?<![:'\"\w/])//.*$", "", text, flags=re.M)


def _app_files(root: Path) -> list[Path]:
    files = [root / name for name in APP_FILES if (root / name).is_file()]
    for directory in APP_DIRS:
        base = root / directory
        if base.is_dir():
            files.extend(p for p in sorted(base.rglob("*")) if p.suffix in APP_SUFFIXES)
    return [
        p
        for p in files
        if "node_modules" not in p.parts
        and not re.search(r"\.(test|spec)\.[jt]s$", p.name)  # tests do not ship
    ]


def _vendored_files(base: Path) -> list[Path]:
    out: list[Path] = []
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base).as_posix()
        if not path.is_file() or path.suffix not in {".js", ".mjs", ".html", ".css"}:
            continue
        if rel.startswith(NOT_RUNTIME):
            continue
        out.append(path)
    return out


def asset_problems(root: Path = ROOT, *, require_checkout: bool = False) -> list[str]:
    problems: list[str] = []
    for path in _app_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        for loader in APP_LOADERS:
            if loader.search(_strip_comments(text) if path.suffix in {".ts", ".js"} else text):
                problems.append(f"{path.relative_to(root)}: loads from a third-party origin")
                break
    base = root / SUBMODULE
    if not base.is_dir() or not any(base.iterdir()):
        if require_checkout:
            problems.append(f"{SUBMODULE} is not checked out (checkout with `submodules: true`)")
        return problems
    for path in _vendored_files(base):
        rel = path.relative_to(root)
        text = path.read_text(encoding="utf-8", errors="replace")
        if rel.as_posix().startswith(f"{SUBMODULE}/vendor/pdfjs/"):
            continue  # pdf.js is not registered in M5 (D7); its licence is in THIRD_PARTY_NOTICES
        code = _strip_comments(text) if path.suffix == ".js" else text
        hosts = {
            host
            for host in URL_LITERAL.findall(code)
            if host not in NAMESPACE_HOSTS and urlsplit(f"https://{host}").hostname
        }
        if hosts:
            problems.append(f"{rel}: absolute URL to {sorted(hosts)}")
        if rel.parent.as_posix() == f"{SUBMODULE}/vendor":
            # zip.js still contains its Worker code; it is inert while view.js turns workers off.
            continue
        if NETWORK_PRIMITIVES.search(code):
            problems.append(f"{rel}: network primitive (worker, socket, beacon or script import)")
    view = base / "view.js"
    if view.is_file() and "useWebWorkers: false" not in view.read_text(encoding="utf-8"):
        problems.append(f"{SUBMODULE}/view.js no longer disables zip.js workers (useWebWorkers)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pin")
    assets = sub.add_parser("assets")
    assets.add_argument("--require-checkout", action="store_true")
    args = parser.parse_args(argv)
    problems = (
        pin_problems()
        if args.command == "pin"
        else asset_problems(require_checkout=args.require_checkout)
    )
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if not problems:
        print(f"ok: {args.command}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
