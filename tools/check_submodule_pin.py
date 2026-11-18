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
import hashlib
import re
import subprocess
import sys
from pathlib import Path

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
# Third-party bundles shipped in the submodule's vendor/ : pinned by SHA-256 (commit above). Any
# other script in vendor/ is refused. fflate and zip.js are loaded; pdf.js is not (ADR-0055, D7).
VENDOR_PINS = {
    "vendor/zip.js": "2a0d43157e4a447c6e43351225758795b75a33e8dfec10c6767248df2182949a",
    "vendor/fflate.js": "e63bcc21f83a43e5f71d5cbefd351ff4d4dbd57bef8b29ad65c8bd65752f6e6b",
    "vendor/pdfjs/pdf.mjs": "70f8fd59e1c947d32ebfc7e5b101cc164d305fe98c44523355355ab79875d9e9",
    "vendor/pdfjs/pdf.worker.mjs": "682770dbc7ac4d94017b79628aa8c231934415f80a76ed99be09ed743912a0ab",  # noqa: E501
}
VENDOR_DATA_SUFFIXES = {".bcmap", ".pfb", ".ttf", ".css", ".map", ""}
# Every way for the library's own code to reach outside the page or load code. The count of each
# primitive per file is pinned to the audited commit: an update that adds one stops here and goes
# to the manual review (docs/dependencies.md). This is an alarm, not the barrier: the barrier is
# the reader CSP (connect-src 'self', worker-src 'none', ADR-0055).
PRIMITIVES = {
    "fetch": re.compile(r"\bfetch\s*\("),
    "open": re.compile(r"\bopen\s*\("),
    "import()": re.compile(r"\bimport\s*\("),
    "importScripts": re.compile(r"\bimportScripts\b"),
    "XMLHttpRequest": re.compile(r"\bXMLHttpRequest\b"),
    "WebSocket": re.compile(r"\bWebSocket\b|\bwss?:"),
    "EventSource": re.compile(r"\bEventSource\b"),
    "sendBeacon": re.compile(r"\bsendBeacon\b"),
    "Worker": re.compile(r"\b(?:Shared)?Worker\b"),
    "src=": re.compile(r"\.src\s*="),
    "srcdoc": re.compile(r"\bsrcdoc\b"),
    "location": re.compile(r"\blocation\b"),
    "eval": re.compile(r"\beval\s*\(|\bnew\s+Function\b"),
    "innerHTML": re.compile(r"\b(?:inner|outer)HTML\b|\binsertAdjacentHTML\b|\bdocument\.write"),
    "node-move": re.compile(
        r"\b(?:cloneNode|importNode|adoptNode|extractContents|cloneContents|insertNode"
        r"|createContextualFragment)\b"
    ),
}
# Audited at the pinned commit (docs/dependencies.md, audit): {file: {primitive: count}}.
INVENTORY: dict[str, dict[str, int]] = {
    "epub.js": {"src=": 1},
    "fb2.js": {"fetch": 1, "innerHTML": 2},
    "fixed-layout.js": {"open": 1, "src=": 1},
    "footnotes.js": {"node-move": 1, "open": 1},
    "mobi.js": {"innerHTML": 1, "open": 3, "src=": 2},
    "opds.js": {"import()": 1, "innerHTML": 1},
    "paginator.js": {"innerHTML": 1, "open": 1, "src=": 1},
    "pdf.js": {"fetch": 1, "node-move": 1},
    "progress.js": {"location": 1},
    "quote-image.js": {"innerHTML": 1, "node-move": 1, "src=": 1},
    "reader.js": {"import()": 1, "location": 3, "open": 6, "src=": 1},
    "tts.js": {"node-move": 3},
    "view.js": {"fetch": 1, "import()": 13, "open": 4},
}

URL_LITERAL = re.compile(r"(?<![\w.-])https?://([A-Za-z0-9._-]+)")
# How an application source loads something from an absolute (or protocol-relative) URL.
APP_LOADERS = (
    re.compile(
        r"""\b(?:src|href|action|poster|data|srcset)\s*=\s*["'`]\s*(?:https?:)?//""",
        re.I,
    ),
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
APP_SUFFIXES = {
    ".ts",
    ".js",
    ".mjs",
    ".svelte",
    ".html",
    ".css",
    ".json",
    ".webmanifest",
}


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
    gitlinks = _gitlinks(root)
    linked = gitlinks.get(SUBMODULE)
    if sorted(gitlinks) != [SUBMODULE]:
        problems.append(f"the only gitlink must be {SUBMODULE}, found {sorted(gitlinks)}")
    documented = documented_commit(root)
    if documented is None:
        problems.append(f"{DEPENDENCIES}: no line 'Commit épinglé : `<sha>`'")
    elif not FULL_SHA.fullmatch(documented):
        problems.append(f"{DEPENDENCIES}: the pinned commit must be 40 lowercase hex digits")
    if linked and documented and linked != documented:
        problems.append(
            f"{DEPENDENCIES} documents {documented} but the gitlink is {linked}: "
            "an update is a reviewed change of both, in the same commit"
        )
    if linked:
        problems.extend(_other_mentions(root, linked))
    problems.extend(_gitmodules_problems(root))
    return problems


def _gitlinks(root: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in _git(root, "ls-files", "-s").splitlines():
        meta, _, path = line.partition("\t")
        mode, sha, _stage = meta.split(" ")
        if mode == "160000":
            out[path] = sha
    return out


def _other_mentions(root: Path, linked: str) -> list[str]:
    """Every full commit written in the foliate-js sections and notices is the gitlink."""
    problems: list[str] = []
    sections = re.findall(
        r"^## foliate-js.*?(?=^## (?!foliate-js)|\Z)",
        (root / DEPENDENCIES).read_text(encoding="utf-8") if (root / DEPENDENCIES).exists() else "",
        re.M | re.S,
    )
    texts = {DEPENDENCIES: "\n".join(sections)}
    notices = root / "THIRD_PARTY_NOTICES"
    if notices.exists():
        texts["THIRD_PARTY_NOTICES"] = notices.read_text(encoding="utf-8")
    for name, text in texts.items():
        for sha in re.findall(r"\b[0-9a-f]{40}\b", text):
            if sha != linked:
                problems.append(f"{name} mentions commit {sha}, the gitlink is {linked}")
    if "THIRD_PARTY_NOTICES" in texts and linked not in texts["THIRD_PARTY_NOTICES"]:
        problems.append("THIRD_PARTY_NOTICES does not name the pinned commit")
    return problems


def _gitmodules_problems(root: Path) -> list[str]:
    """Git's own reading of `.gitmodules`: exactly one path and one official URL, nothing else."""
    if not (root / ".gitmodules").exists():
        return [".gitmodules missing"]
    raw = _git(root, "config", "-f", ".gitmodules", "--null", "--list")
    entries = [item.partition("\n") for item in raw.split("\0") if item]
    pairs = [(key, value) for key, _, value in entries]
    expected = {
        f"submodule.{SUBMODULE}.path": SUBMODULE,
        f"submodule.{SUBMODULE}.url": OFFICIAL_URL,
    }
    problems = [
        f".gitmodules: '{key}' is not allowed (only path and the official url)"
        for key, _ in pairs
        if key not in expected
    ]
    for key, want in expected.items():
        values = [value for k, value in pairs if k == key]
        if values != [want]:
            problems.append(f".gitmodules: {key} must be exactly {want!r}, found {values}")
    return problems


def _code(text: str) -> str:
    """Drop comment-only lines and trailing line comments (never inside a string or a URL)."""
    out: list[str] = []
    in_block = False
    for line in text.splitlines():
        stripped = line.strip()
        if in_block:
            if "*/" in stripped:
                in_block = False
                stripped = stripped.split("*/", 1)[1].strip()
            else:
                continue
        if stripped.startswith("/*"):
            if "*/" not in stripped:
                in_block = True
            continue
        if stripped.startswith("//"):
            continue
        out.append(re.sub(r"\s//(?=[^'\"`]*$).*$", "", line))
    return "\n".join(out)


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


def _runtime_files(base: Path) -> list[Path]:
    """The library's own code: root and ui/ scripts, no tests, CI or build files, no vendor/."""
    out: list[Path] = []
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base).as_posix()
        if not path.is_file() or rel.startswith(("vendor/", ".git/", *NOT_RUNTIME)):
            continue
        if path.suffix in {".js", ".mjs", ".html", ".css", ".svg"}:
            out.append(path)
    return out


def inventory(base: Path) -> dict[str, dict[str, int]]:
    found: dict[str, dict[str, int]] = {}
    for path in _runtime_files(base):
        if path.suffix not in {".js", ".mjs", ".html"}:
            continue
        code = _code(path.read_text(encoding="utf-8", errors="replace"))
        counts = {name: len(rx.findall(code)) for name, rx in PRIMITIVES.items()}
        found[path.relative_to(base).as_posix()] = {k: v for k, v in counts.items() if v}
    return found


def _vendor_problems(base: Path, prefix: str) -> list[str]:
    problems: list[str] = []
    vendor = base / "vendor"
    if not vendor.is_dir():
        return problems
    for path in sorted(vendor.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(base).as_posix()
        pinned = VENDOR_PINS.get(rel)
        if pinned is not None:
            if hashlib.sha256(path.read_bytes()).hexdigest() != pinned:
                problems.append(f"{prefix}/{rel}: differs from the audited bundle (SHA-256)")
        elif path.suffix not in VENDOR_DATA_SUFFIXES and path.name != "LICENSE":
            problems.append(f"{prefix}/{rel}: unexpected script in vendor/ (not pinned)")
    return problems


def asset_problems(root: Path = ROOT, *, require_checkout: bool = False) -> list[str]:
    problems: list[str] = []
    for path in _app_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        if path.suffix in {".ts", ".js"}:
            text = _code(text)
        if any(loader.search(text) for loader in APP_LOADERS):
            problems.append(f"{path.relative_to(root)}: loads from a third-party origin")
    base = root / SUBMODULE
    if not base.is_dir() or not any(base.iterdir()):
        if require_checkout:
            problems.append(f"{SUBMODULE} is not checked out (checkout with `submodules: true`)")
        return problems
    for path in _runtime_files(base):
        rel = path.relative_to(root)
        code = _code(path.read_text(encoding="utf-8", errors="replace"))
        hosts = {h for h in URL_LITERAL.findall(code) if h not in NAMESPACE_HOSTS}
        if hosts:
            problems.append(f"{rel}: absolute URL to {sorted(hosts)}")
    for name, counts in inventory(base).items():
        audited = INVENTORY.get(name, {})
        more = {k: v for k, v in counts.items() if v > audited.get(k, 0)}
        if more:  # fewer of them is never a risk
            problems.append(
                f"{SUBMODULE}/{name}: more primitives than audited {audited}: {more} "
                "(review the diff, then update INVENTORY)"
            )
    problems.extend(_vendor_problems(base, SUBMODULE))
    view = base / "view.js"
    if view.is_file() and not re.search(
        r"^\s*configure\(\s*\{\s*useWebWorkers:\s*false",
        _code(view.read_text("utf-8")),
        re.M,
    ):
        problems.append(f"{SUBMODULE}/view.js no longer disables zip.js workers (useWebWorkers)")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("pin")
    assets = sub.add_parser("assets")
    assets.add_argument("--require-checkout", action="store_true")
    sub.add_parser("inventory", help="print the primitives found, to review then pin in INVENTORY")
    args = parser.parse_args(argv)
    if args.command == "inventory":
        import json

        print(json.dumps(inventory(ROOT / SUBMODULE), indent=4, sort_keys=True))
        return 0
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
