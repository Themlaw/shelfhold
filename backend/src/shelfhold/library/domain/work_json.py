"""The format of `work.json` (ENF-OPS-013, ENF-OPS-020, ADR-0039).

A read projection of the database for one Work, in `/data/meta/works/<ulid[0:2]>/<ulid>.json`:
its title and status, the titles it is known under, and the identifiers of its Editions and Units.
It carries **no secret, no key, no token and no path** (nothing of the file system at all): a
person who can read `/data` learns nothing from it that the catalog does not already say.

The rules are those of `unit.json`: `schema_version` is `major.minor`; a reader keeps the fields it
does not know when it rewrites; a file of a newer version, minor or major, is read-only and is
never rewritten; the database is the authority and replaces a corrupt file.
"""

import json
from dataclasses import dataclass
from typing import Any, Final

from shelfhold.library.domain.unit_json import (
    MAX_BYTES,
    NewerFormatError,
    UnitJsonCorruptError,
    _no_duplicate_keys,
    _parse_version,
)

__all__ = ["WORK_JSON_VERSION", "WorkJson"]

WORK_JSON_VERSION: Final = "1.0"
_CURRENT = (1, 0)
_OWNED: Final = (
    "schema_version", "work_id", "kind", "title", "sort_title", "status", "first_published",
    "date_precision", "titles", "edition_ids", "unit_ids",
)  # fmt: skip


@dataclass(frozen=True)
class WorkJson:
    fields: dict[str, Any]
    version: str
    read_only: bool = False

    @classmethod
    def parse(cls, raw: bytes) -> "WorkJson":
        if len(raw) > MAX_BYTES:
            raise UnitJsonCorruptError(f"larger than {MAX_BYTES} bytes")
        try:
            fields = json.loads(raw, object_pairs_hook=_no_duplicate_keys)
        except (ValueError, RecursionError) as error:
            raise UnitJsonCorruptError(type(error).__name__) from None
        if not isinstance(fields, dict) or "schema_version" not in fields:
            raise UnitJsonCorruptError("not an object with a schema_version")
        found = _parse_version(fields["schema_version"])
        if found > _CURRENT:  # the version first: a newer file may use what we do not know
            return cls(fields, f"{found[0]}.{found[1]}", read_only=True)
        return cls(fields, WORK_JSON_VERSION)

    @classmethod
    def new(cls, known: dict[str, Any]) -> "WorkJson":
        return cls({}, WORK_JSON_VERSION).merge(known)

    def merge(self, known: dict[str, Any]) -> "WorkJson":
        """The same document with the fields this build owns replaced; the others are kept."""
        if self.read_only:
            raise NewerFormatError(f"work.json {self.version} is newer than {WORK_JSON_VERSION}")
        merged = {**self.fields, **{k: v for k, v in known.items() if k in _OWNED}}
        merged["schema_version"] = WORK_JSON_VERSION
        return WorkJson(merged, WORK_JSON_VERSION)

    def dumps(self) -> bytes:
        if self.read_only:
            raise NewerFormatError(f"work.json {self.version} is newer than {WORK_JSON_VERSION}")
        return (
            json.dumps(self.fields, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        ).encode()
