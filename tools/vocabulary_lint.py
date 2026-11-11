#!/usr/bin/env python3
"""Vocabulary lint of the rights wording (ENF-LEG-008, ADR-0032).

Gutenberg and Standard Ebooks are free of rights in the United States, which is not a legal
source everywhere. Published docs, the French message catalog and the French UI catalog must say
"libre de droits aux États-Unis" on every line that names one of the two, and never "source
légale" there. In the two catalogs the phrase "source légale" is banned outright, because a
message can lose its context. The ADRs and the spec quote the rule and are not scanned.

The proxy rule (EF-SRC-013, J4, U2, ENF-LEG-006): a line of the docs or of the catalogs that names
a proxy never presents it as a way to reach a blocked site or to get around a blocking measure. A
per-source proxy routes the household's network, nothing else.

Standard library only. Exit 1 and one line per offence when the wording is wrong.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

NAMED = re.compile(r"gutenberg|standard\s+ebooks", re.IGNORECASE)
BANNED = re.compile(r"sources?\s+l[ée]gales?", re.IGNORECASE)
REQUIRED = re.compile(r"aux\s+[ÉE]tats-Unis", re.IGNORECASE)
CATALOGS = ("backend/src/shelfhold/locales/fr.toml", "frontend/src/lib/i18n/fr.json")
DOCS = ("README.md", "ROADMAP.md", "docs/admin", "docs/legal", "docs/governance", "docs/reference")
PROXY = re.compile(r"\bprox(?:y|ies)\b", re.IGNORECASE)
CIRCUMVENTION = re.compile(
    r"contourn|d[ée]bloqu|\bbloqu[ée]e?s?\b|blocage|censu[r]|g[ée]o-?(?:restr|bloc)|interdit"
    r"|circumvent|bypass|unblock|blocked|get\s+around|evad|geo-?(?:block|restrict)",
    re.IGNORECASE,
)


def _files(root: Path, entries: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for entry in entries:
        path = root / entry
        if path.is_dir():
            found += sorted(path.rglob("*.md"))
        elif path.is_file():
            found.append(path)
    return found


def scan(root: Path) -> list[str]:
    offences: list[str] = []
    for path in _files(root, CATALOGS):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if BANNED.search(line):
                offences.append(f"{path.relative_to(root)}:{number}: 'source légale' is banned")
    for path in _files(root, DOCS):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not NAMED.search(line):
                continue
            if BANNED.search(line) or not REQUIRED.search(line):
                offences.append(
                    f"{path.relative_to(root)}:{number}: say 'libre de droits aux États-Unis'"
                )
    # a line of a catalog that names one of the two must also carry the country
    for path in _files(root, CATALOGS):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if NAMED.search(line) and not REQUIRED.search(line) and not BANNED.search(line):
                offences.append(f"{path.relative_to(root)}:{number}: say 'aux États-Unis'")
    for path in [*_files(root, CATALOGS), *_files(root, DOCS)]:
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PROXY.search(line) and CIRCUMVENTION.search(line):
                offences.append(
                    f"{path.relative_to(root)}:{number}: a proxy is never a way around a block"
                )
    return offences


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    offences = scan(root)
    for offence in offences:
        print(offence)
    return 1 if offences else 0


if __name__ == "__main__":
    sys.exit(main())
