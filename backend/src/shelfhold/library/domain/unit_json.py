"""The format of `unit.json` (ENF-OPS-013, ADR-0039).

`schema_version` is `major.minor`. A reader keeps what it does not know: a rewrite changes the
fields this build owns and leaves every other field as it found it. A file older than this build
is migrated in memory when it is read (never on disk: the projection rewrites it when it next has
something to say). A file of a newer version, minor or major, is read-only: it can be read, never
rewritten, and `NewerFormatError` is what any attempt gets. Forward migration only.

`UNIT_JSON_VERSION` 1.1 added `file_hashes` (the hash of each rendered file per destination, G1)
and `store_provenance` on every content. 1.0 had neither; a bare integer `1` is read as 1.0.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Final

UNIT_JSON_VERSION: Final = "1.1"
_CURRENT = (1, 1)
_OWNED: Final = ("schema_version", "unit_id", "edition_id", "contents", "file_hashes")
_VERSION = re.compile(r"(\d+)(?:\.(\d+))?")
_HASH = re.compile(r"sha256:[0-9a-f]{64}")
_HASH_FIELDS: Final = ("original_sha256", "content_hash", "derived_hash", "file_hash")


class UnitJsonCorruptError(ValueError):
    """Not readable at all: the database is the authority and rewrites it."""


class NewerFormatError(RuntimeError):
    """A format newer than this build: read-only (`reconcile.drift` warns, nothing is written)."""


def _parse_version(value: object) -> tuple[int, int]:
    text = str(value) if isinstance(value, int) and not isinstance(value, bool) else value
    match = _VERSION.fullmatch(text) if isinstance(text, str) else None
    if match is None:
        raise UnitJsonCorruptError(f"unusable schema_version {value!r}")
    major, minor = int(match[1]), int(match[2] or 0)
    if major < 1:
        raise UnitJsonCorruptError(f"unknown schema_version {value!r}")
    return major, minor


def _check_hashes(node: object) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _HASH_FIELDS and value is not None:
                if not (isinstance(value, str) and _HASH.fullmatch(value)):
                    raise UnitJsonCorruptError(f"{key} must be sha256:<hex>")
            else:
                _check_hashes(value)
    elif isinstance(node, list):
        for item in node:
            _check_hashes(item)


@dataclass(frozen=True)
class UnitJson:
    fields: dict[str, Any]
    version: str
    read_only: bool = False
    migrated_from: str | None = None

    @classmethod
    def parse(cls, raw: bytes) -> "UnitJson":
        try:
            fields = json.loads(raw)
        except ValueError as error:
            raise UnitJsonCorruptError(str(error)) from None
        if not isinstance(fields, dict) or "schema_version" not in fields:
            raise UnitJsonCorruptError("not an object with a schema_version")
        found = _parse_version(fields["schema_version"])
        _check_hashes(fields)
        if found > _CURRENT:
            return cls(fields, f"{found[0]}.{found[1]}", read_only=True)
        if found == _CURRENT:
            return cls(fields, UNIT_JSON_VERSION)
        return cls(_migrate(fields), UNIT_JSON_VERSION, migrated_from=f"{found[0]}.{found[1]}")

    @classmethod
    def new(cls, known: dict[str, Any]) -> "UnitJson":
        return cls({}, UNIT_JSON_VERSION).merge(known)

    def merge(self, known: dict[str, Any]) -> "UnitJson":
        """The same document with the fields this build owns replaced; the others are kept."""
        if self.read_only:
            raise NewerFormatError(f"unit.json {self.version} is newer than {UNIT_JSON_VERSION}")
        merged = {**self.fields, **{k: v for k, v in known.items() if k in _OWNED}}
        merged["schema_version"] = UNIT_JSON_VERSION
        _check_hashes(merged)
        return UnitJson(merged, UNIT_JSON_VERSION)

    def dumps(self) -> bytes:
        if self.read_only:
            raise NewerFormatError(f"unit.json {self.version} is newer than {UNIT_JSON_VERSION}")
        return (
            json.dumps(self.fields, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode()


def _migrate(fields: dict[str, Any]) -> dict[str, Any]:
    """1.0 -> 1.1: the only provenance 1.0 knew is `received`; no destination hash was recorded."""
    migrated = dict(fields)
    migrated["schema_version"] = UNIT_JSON_VERSION
    migrated.setdefault("file_hashes", [])
    migrated["contents"] = [
        {"store_provenance": "received", **content} for content in fields.get("contents", [])
    ]
    return migrated
