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
from datetime import UTC, datetime, timedelta
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
# Every file of the submodule's vendor/ (bundles, pdf.js, data) is pinned by SHA-256 in this
# manifest, whatever its extension: an extra, missing or changed file stops the update.
VENDOR_MANIFEST = "tools/foliate-vendor.sha256"
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
    "import-from": re.compile(r"\bfrom\s*['\"]|^\s*import\s*['\"]", re.MULTILINE),
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
    "epub.js": {"import-from": 1, "src=": 1},
    "fb2.js": {"fetch": 1, "innerHTML": 2},
    "fixed-layout.js": {"open": 1, "src=": 1},
    "footnotes.js": {"node-move": 1, "open": 1},
    "mobi.js": {"innerHTML": 1, "open": 3, "src=": 2},
    "opds.js": {"import()": 1, "innerHTML": 1},
    "paginator.js": {"innerHTML": 1, "open": 1, "src=": 1},
    "pdf.js": {"fetch": 1, "import-from": 1, "node-move": 1},
    "progress.js": {"location": 1},
    "quote-image.js": {"innerHTML": 1, "node-move": 1, "src=": 1},
    "reader.js": {"import()": 1, "import-from": 4, "location": 3, "open": 6, "src=": 1},
    "tts.js": {"node-move": 3},
    "view.js": {"fetch": 1, "import()": 13, "import-from": 4, "open": 4},
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


def min_age_problems(root: Path, days: int, *, now: datetime | None = None) -> list[str]:
    """The pinned commit is at least `days` old: a release ages before it is proposed."""
    linked = gitlink_commit(root)
    if linked is None:
        return [f"{SUBMODULE} is not a gitlink"]
    stamp = _git(root / SUBMODULE, "show", "-s", "--format=%cI", linked).strip()
    if not stamp:
        return [f"{SUBMODULE}: commit {linked} not found (check out the submodule)"]
    age = (now or datetime.now(UTC)) - datetime.fromisoformat(stamp)
    if age < timedelta(days=days):
        return [f"pinned commit {linked} is {age.days} days old, the minimum is {days}"]
    return []


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


_REGEX_AFTER = frozenset("(,=:[!&|?{};+-*%<>~^")


def _code(text: str, *, css: bool = False) -> str:
    """The text without its comments (a small tokenizer: strings, regexes and comments).

    Whatever follows a closed comment on the same line is kept: `/**/ fetch(u)` is code.
    """
    out: list[str] = []
    i, n = 0, len(text)
    last = ""  # last significant character of the code, to tell a regex from a division
    while i < n:
        c, two = text[i], text[i : i + 2]
        if two == "/*":
            j = text.find("*/", i + 2)
            i = n if j < 0 else j + 2
            out.append(" ")
        elif two == "//" and not css:
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif c in "'\"`":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            literal = text[i : j + 1]
            if c == "`":  # embedded CSS or HTML: its own comments are not code either
                literal = re.sub(r"/\*.*?\*/", " ", literal, flags=re.DOTALL)
            out.append(literal)
            i, last = j + 1, c
        elif c == "/" and not css and (last == "" or last in _REGEX_AFTER):
            j, in_class = i + 1, False
            while j < n and text[j] != "\n" and (in_class or text[j] != "/"):
                if text[j] == "\\":
                    j += 1
                elif text[j] == "[":
                    in_class = True
                elif text[j] == "]":
                    in_class = False
                j += 1
            out.append(text[i : j + 1])
            i, last = j + 1, ")"
        else:
            out.append(c)
            if not c.isspace():
                last = c
            i += 1
    return "".join(out)


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


def _vendor_files(base: Path) -> dict[str, Path]:
    vendor = base / "vendor"
    if not vendor.is_dir():
        return {}
    return {p.relative_to(base).as_posix(): p for p in sorted(vendor.rglob("*")) if p.is_file()}


def vendor_digests(base: Path) -> dict[str, str]:
    return {
        rel: hashlib.sha256(path.read_bytes()).hexdigest()
        for rel, path in _vendor_files(base).items()
    }


def _manifest(root: Path) -> dict[str, str]:
    path = root / VENDOR_MANIFEST
    if not path.exists():
        path = ROOT / VENDOR_MANIFEST  # synthetic trees are checked against the real manifest
    out: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        digest, _, rel = line.partition("  ")
        if digest:
            out[rel] = digest
    return out


def _vendor_problems(root: Path, base: Path, prefix: str) -> list[str]:
    problems: list[str] = []
    files = _vendor_files(base)
    if not files:
        return problems
    manifest = _manifest(root)
    actual = vendor_digests(base)
    for rel in sorted(actual.keys() - manifest.keys()):
        problems.append(f"{prefix}/{rel}: file of vendor/ that is not pinned")
    for rel in sorted(manifest.keys() - actual.keys()):
        problems.append(f"{prefix}/{rel}: pinned file of vendor/ is missing")
    for rel in sorted(actual.keys() & manifest.keys()):
        if actual[rel] != manifest[rel]:
            problems.append(f"{prefix}/{rel}: differs from the audited file (SHA-256)")
    # Sweep every file of vendor/ whatever its extension: no remote load, text or not.
    for rel, path in files.items():
        if path.suffix == ".map":
            continue  # source maps: JSON text of the sources, never executed, pinned above
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            continue  # binary data (fonts, glyph maps)
        code = _code(text, css=path.suffix == ".css")
        if any(loader.search(code) for loader in APP_LOADERS):
            problems.append(f"{prefix}/{rel}: loads from a third-party origin")
    return problems


_CONFIGURE = re.compile(r"\bconfigure\s*\(")
_WORKERS_OFF = re.compile(r"\bconfigure\(\s*\{\s*useWebWorkers\s*:\s*false\s*,?\s*\}\s*\)")


def _worker_problems(base: Path) -> list[str]:
    """zip.js workers stay off: the only `configure` is the one that turns them off."""
    problems: list[str] = []
    seen_off = False
    for path in _runtime_files(base):
        if path.suffix not in {".js", ".mjs", ".html"}:
            continue
        code = _code(path.read_text(encoding="utf-8", errors="replace"))
        rel = path.relative_to(base).as_posix()
        calls = len(_CONFIGURE.findall(code))
        off = len(_WORKERS_OFF.findall(code))
        if calls != off:
            problems.append(
                f"{SUBMODULE}/{rel}: a `configure` call other than useWebWorkers: false"
            )
        if len(re.findall(r"\buseWebWorkers\b", code)) != off:
            problems.append(f"{SUBMODULE}/{rel}: useWebWorkers set to something else than false")
        if rel == "view.js" and off:
            seen_off = True
    if (base / "view.js").is_file() and not seen_off:
        problems.append(f"{SUBMODULE}/view.js no longer disables zip.js workers (useWebWorkers)")
    return problems


def asset_problems(root: Path = ROOT, *, require_checkout: bool = False) -> list[str]:
    problems: list[str] = []
    for path in _app_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        # Both readings: as script (no `//` comment hides code) and as stylesheet (an unquoted
        # `url(https://…)` is not a comment).
        views = (_code(text), _code(text, css=True))
        if any(loader.search(view) for view in views for loader in APP_LOADERS):
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
    problems.extend(_vendor_problems(root, base, SUBMODULE))
    problems.extend(_worker_problems(base))
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    pin = sub.add_parser("pin")
    pin.add_argument(
        "--min-age",
        type=int,
        metavar="DAYS",
        help="also require the pinned commit to be at least DAYS old (needs the checkout)",
    )
    assets = sub.add_parser("assets")
    assets.add_argument("--require-checkout", action="store_true")
    sub.add_parser("inventory", help="print the primitives found, to review then pin in INVENTORY")
    sub.add_parser("vendor-manifest", help="print the SHA-256 manifest of vendor/ (to review)")
    args = parser.parse_args(argv)
    if args.command == "inventory":
        import json

        print(json.dumps(inventory(ROOT / SUBMODULE), indent=4, sort_keys=True))
        return 0
    if args.command == "vendor-manifest":
        for rel, digest in vendor_digests(ROOT / SUBMODULE).items():
            print(f"{digest}  {rel}")
        return 0
    if args.command == "pin":
        problems = pin_problems()
        if args.min_age is not None:
            problems.extend(min_age_problems(ROOT, args.min_age))
    else:
        problems = asset_problems(require_checkout=args.require_checkout)
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if not problems:
        print(f"ok: {args.command}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
