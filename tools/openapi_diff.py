#!/usr/bin/env python3
"""Full OpenAPI document comparison with a rule for breaking changes (M1a L58, EF-INT-001).

Standard library only. `diff(old, new)` walks the two documents completely (paths, methods,
parameters, bodies, responses, component schemas, security, info), in both directions: every
added, removed or changed node is a `Change`. A change is *breaking* for an API client when:
- a path, a method or a documented response status disappears;
- a parameter disappears, or a required one appears, or a parameter's `required`/`in`/schema
  becomes stricter;
- a request body becomes required;
- a schema property disappears, a property becomes required, or a constraint keyword
  (`type`, `format`, `$ref`, `enum` value removed, `anyOf`/`oneOf`/`allOf`, `items`,
  `additionalProperties`, bounds, `pattern`, `nullable`, `const`) is changed or removed;
- a security requirement or scheme appears or changes.
Everything else (descriptions, titles, examples, new paths, new optional fields, `info.version`)
is a compatible change: it still fails the snapshot test, but it is reported as such.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_HTTP = {"get", "put", "post", "delete", "options", "head", "patch", "trace"}
_CONSTRAINTS = {
    "type", "format", "$ref", "anyOf", "oneOf", "allOf", "items", "additionalProperties",
    "nullable", "const", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum",
    "minLength", "maxLength", "pattern", "minItems", "maxItems", "discriminator",
}  # fmt: skip


@dataclass(frozen=True)
class Change:
    pointer: str  # JSON pointer into the document, `/` escaped as `~1`
    kind: str  # "added" | "removed" | "changed"
    breaking: bool
    why: str

    def __str__(self) -> str:
        flag = "BREAKING" if self.breaking else "compatible"
        return f"{flag} {self.kind} {self.pointer}: {self.why}"


def _esc(part: str) -> str:
    return part.replace("~", "~0").replace("/", "~1")


def _is_schema_node(parts: tuple[str, ...]) -> bool:
    return "schemas" in parts or "schema" in parts or "properties" in parts or "items" in parts


def _classify(parts: tuple[str, ...], kind: str, old: Any, new: Any) -> tuple[bool, str]:
    """(breaking, why) for one structural change at `parts` (unescaped)."""
    key = parts[-1] if parts else ""
    parent = parts[-2] if len(parts) > 1 else ""
    if parts[:1] == ("paths",):
        if len(parts) == 2 and kind == "removed":
            return True, "path removed"
        if len(parts) == 3 and parts[2] in _HTTP and kind == "removed":
            return True, "method removed"
        if "responses" in parts and parent == "responses" and kind == "removed":
            return True, f"response {key} removed"
        if key == "parameters":
            return _parameters(kind, old, new)
        if parent == "parameters" or "parameters" in parts[:-1]:
            return _parameter_node(parts, kind, old, new)
        if key == "required" and parent == "requestBody":
            return (new is True, "request body becomes required" if new is True else "relaxed")
        if key == "requestBody" and kind == "added":
            return (bool(new.get("required")) if isinstance(new, dict) else False,
                    "request body added")  # fmt: skip
        if key == "requestBody" and kind == "removed":
            return True, "request body removed"
        if key == "security":
            return (kind != "removed" or bool(old), "security requirement changed")
    if parts[:2] == ("components", "securitySchemes"):
        return (kind != "added", "security scheme changed")
    if parts[:2] == ("components", "schemas") and len(parts) == 3 and kind == "removed":
        return True, "schema removed"
    if _is_schema_node(parts):
        if parent == "properties" and kind == "removed":
            return True, f"property {key} removed"
        if key == "required" and kind in {"added", "changed"}:
            return _required(old, new)
        if parent == "required":
            return (kind == "added", "property becomes required")
        if key == "enum":
            return _enum(old, new, kind)
        if key in _CONSTRAINTS:
            return True, f"constraint `{key}` {kind}"
        if parent == "enum":
            return (kind == "removed", f"enum value {kind}")
    return False, "documentation or compatible addition"


def _parameters(kind: str, old: Any, new: Any) -> tuple[bool, str]:
    if kind == "removed":
        return True, "parameters removed"
    return any(isinstance(p, dict) and p.get("required") for p in new or []), "parameters added"


def _parameter_node(parts: tuple[str, ...], kind: str, old: Any, new: Any) -> tuple[bool, str]:
    key = parts[-1]
    if kind == "removed" and key.isdigit():
        return True, "parameter removed"
    if kind == "added" and key.isdigit():
        return bool(isinstance(new, dict) and new.get("required")), "parameter added"
    if key in {"required", "in", "name"}:
        return (key != "required" or new is True, f"parameter `{key}` {kind}")
    if key in _CONSTRAINTS or "schema" in parts:
        return True, f"parameter schema `{key}` {kind}"
    return False, "documentation"


def _required(old: Any, new: Any) -> tuple[bool, str]:
    added = set(new or []) - set(old or [])
    return bool(added), f"required gains {sorted(added)}" if added else "required relaxed"


def _enum(old: Any, new: Any, kind: str) -> tuple[bool, str]:
    if kind == "added":
        return False, "enum added"
    lost = set(map(str, old or [])) - set(map(str, new or []))
    return bool(lost) or kind == "removed", f"enum values lost {sorted(lost)}"


def diff(old: Any, new: Any, parts: tuple[str, ...] = ()) -> list[Change]:
    out: list[Change] = []
    if isinstance(old, dict) and isinstance(new, dict):
        for k in sorted(old.keys() | new.keys()):
            sub = (*parts, k)
            if k not in new:
                out.append(_change(sub, "removed", old[k], None))
            elif k not in old:
                out.append(_change(sub, "added", None, new[k]))
            else:
                out.extend(diff(old[k], new[k], sub))
    elif isinstance(old, list) and isinstance(new, list) and parts[-1:] != ("enum",):
        if parts[-1:] in {("required",), ("tags",), ("security",)} or not _positional(old, new):
            if old != new:
                out.append(_change(parts, "changed", old, new))
        else:
            for i in range(max(len(old), len(new))):
                sub = (*parts, str(i))
                if i >= len(new):
                    out.append(_change(sub, "removed", old[i], None))
                elif i >= len(old):
                    out.append(_change(sub, "added", None, new[i]))
                else:
                    out.extend(diff(old[i], new[i], sub))
    elif old != new:
        out.append(_change(parts, "changed", old, new))
    return out


def _positional(old: list[Any], new: list[Any]) -> bool:
    return all(isinstance(x, (dict, list)) for x in [*old, *new])


def _change(parts: tuple[str, ...], kind: str, old: Any, new: Any) -> Change:
    breaking, why = _classify(parts, kind, old, new)
    return Change("/" + "/".join(_esc(p) for p in parts), kind, breaking, why)


def breaking(changes: list[Change]) -> list[Change]:
    return [c for c in changes if c.breaking]


def main(argv: list[str]) -> int:
    old = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    new = json.loads(Path(argv[2]).read_text(encoding="utf-8"))
    changes = diff(old, new)
    for c in changes:
        print(c)
    return 1 if breaking(changes) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
