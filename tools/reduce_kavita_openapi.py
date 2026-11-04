"""Reduce a Kavita `openapi.json` to the routes of docs/kavita-routes.md (A16).

Run by hand, once per fixture, never in CI:

    python tools/reduce_kavita_openapi.py RAW.json OUT.json

RAW.json is the file published at the root of the public Kareadita/Kavita repository at the tag of
the version (see backend/tests/fixtures/kavita/openapi/SOURCES.md). The output keeps `openapi`,
`info`, the allow-listed paths and the components they reach, so that the tests can read the DTOs
they build from the same document.
"""

import json
import re
import sys
from pathlib import Path
from typing import Any

ROUTES_DOC = Path(__file__).resolve().parent.parent / "docs" / "kavita-routes.md"
ROW = re.compile(r"^\|\s*(GET|POST|PUT|DELETE)\s*\|\s*`([^`]+)`")


def allowlist() -> list[tuple[str, str]]:
    rows = [ROW.match(line) for line in ROUTES_DOC.read_text("utf-8").splitlines()]
    return [(m.group(1), m.group(2)) for m in rows if m]


def references(node: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "$ref" and isinstance(value, str):
                found.add(value.rsplit("/", 1)[-1])
            else:
                found |= references(value)
    elif isinstance(node, list):
        for item in node:
            found |= references(item)
    return found


def reduce(raw: dict[str, Any]) -> dict[str, Any]:
    paths: dict[str, Any] = {}
    for method, route in allowlist():
        operation = raw["paths"][route][method.lower()]
        paths.setdefault(route, {})[method.lower()] = operation
    schemas = raw["components"]["schemas"]
    wanted: set[str] = set()
    pending = references(paths)
    while pending:
        name = pending.pop()
        if name not in wanted:
            wanted.add(name)
            pending |= references(schemas[name])
    return {
        "openapi": raw["openapi"],
        "info": raw["info"],
        "security": raw.get("security", []),
        "paths": paths,
        "components": {
            "schemas": {name: schemas[name] for name in sorted(wanted)},
            "securitySchemes": raw["components"].get("securitySchemes", {}),
        },
    }


def main(argv: list[str]) -> int:
    source, target = Path(argv[1]), Path(argv[2])
    reduced = reduce(json.loads(source.read_text("utf-8")))
    target.write_text(json.dumps(reduced, indent=1, sort_keys=True) + "\n", "utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
