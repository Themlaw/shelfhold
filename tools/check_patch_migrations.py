"""ENF-OPS-002: a patch release (X.Y.Z with Z > 0) adds no migration.

Usage: python3 tools/check_patch_migrations.py vX.Y.Z   (from a clone that has the tags)

Compares the migration files of the tag with those of the previous tag. A minor or major
release (Z = 0) may add migrations; the first release has no predecessor and passes.
"""

from __future__ import annotations

import re
import subprocess
import sys

VERSIONS_DIR = "backend/src/shelfhold/infrastructure/migrations/versions"
TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")


def parse(tag: str) -> tuple[int, int, int]:
    match = TAG.match(tag)
    if match is None:
        raise ValueError(f"not a release tag: {tag!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def previous_tag(tag: str, tags: list[str]) -> str | None:
    """The highest release tag strictly below `tag`."""
    current = parse(tag)
    older = [t for t in tags if TAG.match(t) and parse(t) < current]
    return max(older, key=parse, default=None)


def added_migrations(before: set[str], after: set[str]) -> list[str]:
    return sorted(after - before)


def violation(tag: str, before: set[str] | None, after: set[str]) -> list[str]:
    """Migrations a patch release must not carry; empty when the rule holds."""
    if parse(tag)[2] == 0 or before is None:
        return []
    return added_migrations(before, after)


def _migrations_at(ref: str) -> set[str]:
    out = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", ref, "--", VERSIONS_DIR],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    return {line for line in out if line.endswith(".py") and not line.endswith("__init__.py")}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_patch_migrations.py vX.Y.Z", file=sys.stderr)
        return 2
    tag = argv[1]
    tags = subprocess.run(
        ["git", "tag", "--list", "v*"], check=True, capture_output=True, text=True
    ).stdout.split()
    previous = previous_tag(tag, tags)
    before = _migrations_at(previous) if previous else None
    added = violation(tag, before, _migrations_at(tag))
    for path in added:
        print(f"error: patch release {tag} adds a migration: {path}", file=sys.stderr)
    return 1 if added else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
