#!/usr/bin/env python3
"""Typed client drift (EF-INT-001): after `open-api/generate.sh`, the schema and the generated
TypeScript client must equal what is committed. A file changed by the generation, or created by
it and never committed, is drift. Generated files are never edited by hand.

Standard library only. Exit 1 and one path per line when something drifted.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

GENERATED = ("open-api", "frontend/src/lib/api")


def drifted(root: Path) -> list[str]:
    done = subprocess.run(  # noqa: S603
        ["git", "status", "--porcelain", "--untracked-files=all", "--", *GENERATED],  # noqa: S607
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    return sorted(line[3:] for line in done.stdout.splitlines())


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent
    found = drifted(root)
    for path in found:
        print(path)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
