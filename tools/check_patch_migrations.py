#!/usr/bin/env python3
"""Published migrations are immutable; a patch release carries none new (ENF-OPS-002, M1a L58).

Standard library only. `tools/published-migrations.toml` freezes the SHA-256 of every Alembic
migration of `main` and the minor line (`since = "X.Y"`) that first shipped it. Then:
- a migration whose fingerprint differs from the list fails, whatever the release;
- a migration missing from the list fails (add it with `--freeze`, which is the review point);
- a list entry whose file is gone fails;
- for a patch release `vX.Y.Z` (Z > 0), any migration that `since` places after X.Y fails.

Usage: check_patch_migrations.py [--tag vX.Y.Z] | --freeze SINCE
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = "backend/src/shelfhold/infrastructure/migrations/versions"
FROZEN = "tools/published-migrations.toml"
_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
_SINCE = re.compile(r"^(\d+)\.(\d+)$")


def fingerprint(path: Path) -> str:
    """SHA-256 of the file, line endings normalised (a checkout must not change it)."""
    data = path.read_bytes().replace(b"\r\n", b"\n")
    return hashlib.sha256(data).hexdigest()


def current(root: Path) -> dict[str, str]:
    return {
        p.name: fingerprint(p) for p in sorted((root / VERSIONS).glob("[0-9][0-9][0-9][0-9]_*.py"))
    }


def load_frozen(root: Path) -> dict[str, dict[str, str]]:
    data = tomllib.loads((root / FROZEN).read_text(encoding="utf-8"))
    return {e["file"]: e for e in data.get("migration", [])}


def _line(since: str) -> tuple[int, int]:
    m = _SINCE.match(since)
    if not m:
        raise ValueError(f"since must be X.Y, got {since!r}")
    return int(m.group(1)), int(m.group(2))


def check(root: Path, tag: str | None = None) -> list[str]:
    problems: list[str] = []
    frozen, now = load_frozen(root), current(root)
    for name, digest in now.items():
        entry = frozen.get(name)
        if entry is None:
            problems.append(f"{name}: not in {FROZEN}; a new migration is frozen with --freeze")
        elif entry["sha256"] != digest:
            problems.append(f"{name}: published migration modified (add a new migration instead)")
    for name in frozen.keys() - now.keys():
        problems.append(f"{name}: published migration removed")
    if tag is not None:
        m = _TAG.match(tag)
        if not m:
            problems.append(f"{tag}: not a vX.Y.Z tag")
        elif int(m.group(3)) > 0:  # a patch release: no migration newer than its minor line
            line = (int(m.group(1)), int(m.group(2)))
            for name in sorted(now):
                entry = frozen.get(name)
                since = entry["since"] if entry else None
                if since is None or _line(since) > line:
                    problems.append(f"{name}: a patch release {tag} must not carry this migration")
    return problems


def freeze(root: Path, since: str) -> None:
    _line(since)
    frozen = load_frozen(root)
    lines = ["# Fingerprints of published migrations (tools/check_patch_migrations.py).\n"]
    for name, digest in current(root).items():
        entry = frozen.get(name)
        lines.append(
            f'\n[[migration]]\nfile = "{name}"\nsha256 = "{entry["sha256"] if entry else digest}"\n'
            f'since = "{entry["since"] if entry else since}"\n'
        )
    (root / FROZEN).write_text("".join(lines), encoding="utf-8")


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--tag", help="release tag vX.Y.Z")
    ap.add_argument("--freeze", metavar="SINCE", help="add the new migrations, as shipped from X.Y")
    ap.add_argument("--root", default=str(ROOT))
    args = ap.parse_args(argv)
    root = Path(args.root)
    if args.freeze:
        freeze(root, args.freeze)
        return 0
    problems = check(root, args.tag)
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
