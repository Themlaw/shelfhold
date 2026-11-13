#!/usr/bin/env python3
"""Parity between the French and English message catalogs (ENF-I18N-001).

The English catalogue must carry exactly the same keys as the French one, in the backend
(`backend/src/shelfhold/locales/{fr,en}.toml`) and in the frontend
(`frontend/src/lib/i18n/{fr,en}.json`). A missing or extra key fails CI.

Standard library only. Exit 1 and one line per offence when the catalogs diverge.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path


def _flatten(table: dict, prefix: str = "") -> set[str]:
    keys: set[str] = set()
    for key, value in table.items():
        dotted = f"{prefix}{key}"
        if isinstance(value, dict):
            keys |= _flatten(value, dotted + ".")
        else:
            keys.add(dotted)
    return keys


def backend_keys(path: Path) -> set[str]:
    with path.open("rb") as handle:
        return _flatten(tomllib.load(handle))


def frontend_keys(path: Path) -> set[str]:
    return set(json.loads(path.read_text(encoding="utf-8")))


def main() -> int:
    root = Path(__file__).resolve().parent.parent
    offences: list[str] = []

    fr_toml = root / "backend/src/shelfhold/locales/fr.toml"
    en_toml = root / "backend/src/shelfhold/locales/en.toml"
    if not en_toml.is_file():
        offences.append(f"{en_toml.relative_to(root)}: missing English catalog")
    else:
        fr_keys = backend_keys(fr_toml)
        en_keys = backend_keys(en_toml)
        for key in sorted(fr_keys - en_keys):
            offences.append(f"backend: key missing in English: {key}")
        for key in sorted(en_keys - fr_keys):
            offences.append(f"backend: key missing in French: {key}")

    fr_json = root / "frontend/src/lib/i18n/fr.json"
    en_json = root / "frontend/src/lib/i18n/en.json"
    if not en_json.is_file():
        offences.append(f"{en_json.relative_to(root)}: missing English catalog")
    else:
        fr_keys = frontend_keys(fr_json)
        en_keys = frontend_keys(en_json)
        for key in sorted(fr_keys - en_keys):
            offences.append(f"frontend: key missing in English: {key}")
        for key in sorted(en_keys - fr_keys):
            offences.append(f"frontend: key missing in French: {key}")

    for offence in offences:
        print(offence)
    return 1 if offences else 0


if __name__ == "__main__":
    sys.exit(main())
