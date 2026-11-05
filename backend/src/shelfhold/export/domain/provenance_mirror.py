"""The per-root provenance mirror, as a pure format (EF-EXP-063, ADR-0046, ADR-0039, I33).

`<root>/.shelfhold/manifest/<shelf_destination>/<export_series>.jsonl`: a header line, then one
line per `export_item`, keyed by `(rel_path, file_hash)`. The lineage (Variant, `remote_unit_id`,
team) is carried **per coverage entry**. The database is the authority; the mirror is a projection
that travels with the files and is read back after a restore, a loss of `/data` or a reinstall.

Rules of the format (`schema_version = "major.minor"`):

- an older version is migrated in memory on read (never rewritten on disk by the read);
- a newer minor is read and rewritten with **every unknown field kept**, at its own version;
- a newer major is read-only (`read_only`, and `dumps` refuses);
- a file is an **untrusted input**: bounded, strictly typed, no NaN; a damaged line is kept
  verbatim (`rejected`) and never grants anything; a line only grants *lineage*
  (`grant_lineage`), when the file's hash matches and its ULIDs are coherent, never an age, a shelf
  or a visibility;
- a line the database does not know stays, flagged `unresolved`, and is never erased by a rebuild
  (`merge_with_database`).

No URL, no domain name of a source and no secret is ever written to it.
"""

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Self

from shelfhold.export.domain.physical_roots import RESERVED_DIR

__all__ = [
    "MAX_LINES",
    "MAX_LINE_BYTES",
    "MAX_MIRROR_BYTES",
    "MIRROR_DIR",
    "MIRROR_SCHEMA_VERSION",
    "Lineage",
    "MirrorDocument",
    "MirrorFormatError",
    "grant_lineage",
    "is_diagnostic_excluded",
    "is_mirror_rel",
    "merge_with_database",
    "mirror_name",
]

MIRROR_DIR: Final = f"{RESERVED_DIR}/manifest"
MIRROR_SCHEMA_VERSION: Final = "1.2"
SUPPORTED_MAJOR: Final = 1
MAX_MIRROR_BYTES: Final = 8 * 1024 * 1024
MAX_LINES: Final = 20_000
MAX_LINE_BYTES: Final = 256 * 1024
MAX_DEPTH: Final = 12
UNRESOLVED: Final = "unresolved"

_VERSION = re.compile(r"^(\d+)\.(\d+)$")
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_SHA = re.compile(r"^sha256:[0-9a-f]{64}$")
_NAME = re.compile(r"^([0-9A-HJKMNP-TV-Z]{26})/([0-9A-HJKMNP-TV-Z]{26})\.jsonl$")


class MirrorFormatError(ValueError):
    """The file cannot be read as a mirror, or is of a newer major and must not be rewritten."""


def mirror_name(shelf_destination_id: str, export_series_id: str) -> str:
    """The file's name inside the manifest folder: two ULIDs, nothing else."""
    name = f"{shelf_destination_id}/{export_series_id}.jsonl"
    if not _NAME.match(name):
        raise ValueError("a mirror file is named by two ULIDs")
    return name


def is_mirror_rel(rel: str) -> bool:
    """`rel` (relative to a root) is the manifest folder or something under it."""
    parts = [p for p in rel.split("/") if p not in ("", ".")]
    return ".." not in parts and parts[:2] == MIRROR_DIR.split("/")


def is_diagnostic_excluded(path: str) -> bool:
    """A diagnostic bundle never holds the mirror (I33)."""
    return f"/{MIRROR_DIR}/" in f"/{path.replace(chr(92), '/').strip('/')}/"


def _reject_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _depth_ok(value: object, depth: int = 0) -> bool:
    if depth > MAX_DEPTH:
        return False
    if isinstance(value, dict):
        return all(_depth_ok(v, depth + 1) for v in value.values())
    if isinstance(value, list):
        return all(_depth_ok(v, depth + 1) for v in value)
    return True


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [k for k, _ in pairs]
    if len(set(keys)) != len(keys):
        raise ValueError("duplicate key")
    return dict(pairs)


def _loads(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_LINE_BYTES:
        raise ValueError("line too long")
    try:
        data = json.loads(line, parse_constant=_reject_constant, object_pairs_hook=_no_duplicates)
    except RecursionError:  # a forged file must never take the export down
        raise ValueError("nested too deep") from None
    if not isinstance(data, dict) or not _depth_ok(data):
        raise ValueError("not a bounded JSON object")
    return data


def _is_ulid(value: object) -> bool:
    return isinstance(value, str) and _ULID.match(value) is not None


def _minor_of(version: str) -> int:
    return _version(version)[1]


def _version(value: object) -> tuple[int, int]:
    match = _VERSION.match(value) if isinstance(value, str) else None
    if match is None:
        raise MirrorFormatError("bad schema_version")
    return int(match.group(1)), int(match.group(2))


# -- migrations (forward only, nothing dropped) ---------------------------------------------------


def _migrate_entry_1_0(old: dict[str, Any]) -> dict[str, Any]:
    """1.0 -> 1.1: one Variant and one team per file become one per coverage entry. Nothing is
    dropped: unknown fields of a Variant or a unit are carried over, and a file without `units`
    keeps its `variant` and `team` as they were."""
    found = old.get("variant")
    variant: dict[str, Any] = found if isinstance(found, dict) else {}
    listed = old.get("units")
    units = [u for u in (listed if isinstance(listed, list) else []) if isinstance(u, dict)]
    entry = {k: v for k, v in old.items() if k not in ("units", "variant", "team")}
    if not units:
        for key in ("variant", "team"):
            if key in old:
                entry[key] = old[key]
    entry["coverage"] = [
        {
            **{
                k: v
                for k, v in variant.items()
                if k not in ("id", "source_key", "sub_source", "remote_unit_id")
            },
            **unit,
            "variant": variant.get("id"),
            "source_key": variant.get("source_key"),
            "sub_source": variant.get("sub_source"),
            "remote_unit_id": variant.get("remote_unit_id"),
            "team": old.get("team"),
        }
        for unit in units
    ]
    entry.setdefault("kind", "entry")
    entry.setdefault("state", "exported")
    entry.setdefault("content_hash_by", None)
    return entry


def _check_entry(entry: dict[str, Any]) -> None:
    """Strict types for the fields this build knows; unknown fields pass through untouched."""
    for key in ("rel_path", "file_hash"):
        if not isinstance(entry.get(key), str) or not entry[key]:
            raise ValueError(f"{key} is required")
    if not _SHA.match(entry["file_hash"]):
        raise ValueError("file_hash is not sha256:<hex>")
    for key in ("content_hash", "derived_hash"):
        if entry.get(key) is not None and not (
            isinstance(entry[key], str) and _SHA.match(entry[key])
        ):
            raise ValueError(f"{key} is not sha256:<hex>")
    for key in ("content_hash_by", "derived_by", "template_ref", "written_at", "state"):
        if entry.get(key) is not None and not isinstance(entry[key], str):
            raise ValueError(f"{key} must be text")
    item = entry.get("export_item")
    if not isinstance(item, dict) or not isinstance(item.get("id"), str):
        raise ValueError("export_item.id is required")
    coverage = entry.get("coverage")
    if not isinstance(coverage, list) or not all(isinstance(c, dict) for c in coverage):
        raise ValueError("coverage must be a list of objects")


@dataclass
class MirrorDocument:
    schema_version: str
    header: dict[str, Any]
    entries: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # damaged lines, kept verbatim

    @property
    def major(self) -> int:
        return _version(self.schema_version)[0]

    @property
    def read_only(self) -> bool:
        return self.major > SUPPORTED_MAJOR

    @classmethod
    def parse(cls, data: bytes, *, strict: bool = False) -> Self:
        if len(data) > MAX_MIRROR_BYTES:
            raise MirrorFormatError("mirror file too large")
        lines = [ln for ln in data.split(b"\n") if ln.strip()]
        if len(lines) > MAX_LINES:
            raise MirrorFormatError("too many lines")
        if not lines:
            raise MirrorFormatError("empty mirror")
        try:
            header = _loads(lines[0])
        except ValueError as error:
            raise MirrorFormatError(f"unreadable header: {error}") from error
        if header.get("kind") != "header":
            raise MirrorFormatError("the first line is the header")
        found = header.get("schema_version")
        major, minor = _version(found)
        migrate = major == SUPPORTED_MAJOR and minor < _minor_of(MIRROR_SCHEMA_VERSION)
        version = str(found)
        entries: list[dict[str, Any]] = []
        rejected: list[str] = []
        for line in lines[1:]:
            try:
                entry = _loads(line)
                if version == "1.0":
                    entry = _migrate_entry_1_0(entry)
                _check_entry(entry)
                entries.append(entry)
            except (ValueError, TypeError, AttributeError, KeyError) as error:
                if strict:
                    raise MirrorFormatError(f"bad line: {error}") from error
                rejected.append(line.decode("utf-8", errors="replace"))
        if migrate:
            version = MIRROR_SCHEMA_VERSION
            header = {**header, "schema_version": version}
        return cls(version, header, entries, rejected)

    def dumps(self) -> bytes:
        if self.read_only:
            raise MirrorFormatError(
                f"mirror format {self.schema_version} is newer than this build: read only"
            )
        head = {**self.header, "kind": "header", "schema_version": self.schema_version}
        lines = [json.dumps(head, sort_keys=False), *(json.dumps(e) for e in self.entries)]
        lines.extend(self.rejected)
        return ("\n".join(lines) + "\n").encode("utf-8")


# -- reading a line: lineage only -------------------------------------------------------------


@dataclass(frozen=True)
class Lineage:
    """Everything a mirror line may grant. No age, no shelf, no visibility (GAP-9-10)."""

    export_item_id: str
    coverage: tuple[Mapping[str, Any], ...]
    template_ref: str | None
    content_hash: str | None
    edition_id: str | None


def grant_lineage(entry: Mapping[str, Any], *, disk_file_hash: str) -> Lineage | None:
    """The lineage of a file, only if the hash of the file on disk is the line's and the ULIDs it
    cites are well formed and consistent (each Unit and Variant once, no repeat)."""
    if entry.get("file_hash") != disk_file_hash or entry.get("state") == UNRESOLVED:
        return None
    item = entry.get("export_item")
    coverage = entry.get("coverage")
    if not isinstance(item, Mapping) or not _is_ulid(item.get("id")):
        return None
    if not isinstance(coverage, list) or not coverage:
        return None
    units: set[str] = set()
    variants: set[str] = set()
    for covered in coverage:
        unit, variant = covered.get("unit"), covered.get("variant")
        if not _is_ulid(unit) or unit in units:
            return None
        if variant is not None and (not _is_ulid(variant) or variant in variants):
            return None
        team = covered.get("team")
        if team is not None and not (isinstance(team, Mapping) and _is_ulid(team.get("id"))):
            return None
        units.add(unit)
        if variant is not None:
            variants.add(variant)
    for key in ("edition", "series"):
        if entry.get(key) is not None and not _is_ulid(entry[key]):
            return None
    return Lineage(
        export_item_id=item["id"],
        coverage=tuple(dict(c) for c in coverage),
        template_ref=entry.get("template_ref"),
        content_hash=entry.get("content_hash"),
        edition_id=entry.get("edition"),
    )


# -- rebuild: the database wins, the unknown stays -------------------------------------------------


def merge_with_database(
    on_disk: MirrorDocument,
    from_database: Iterable[Mapping[str, Any]],
    *,
    header: Mapping[str, Any] | None = None,
) -> MirrorDocument:
    """The projection of the database, plus what the file holds that the database cannot explain.

    A disk line the database now owns (same `export_item`) is replaced by the database's line,
    unknown fields of an identical `(rel_path, file_hash)` merged in. Every other disk line is
    kept, `unresolved`: it may be a series born after the last backup, and only the admin decides
    (GAP-9-03)."""
    owned = [dict(e) for e in from_database]
    by_key = {(e["rel_path"], e["file_hash"]): e for e in on_disk.entries}
    merged: list[dict[str, Any]] = []
    for line in owned:
        previous = by_key.get((line["rel_path"], line["file_hash"]))
        merged.append({**previous, **line} if previous is not None else line)
    owned_items = {e["export_item"]["id"] for e in owned}
    for old in on_disk.entries:
        item = old["export_item"]["id"]
        if item in owned_items:
            continue  # superseded by the database
        merged.append({**old, "state": UNRESOLVED})
    head = {**on_disk.header, **{k: v for k, v in (header or {}).items() if k != "schema_version"}}
    return MirrorDocument(on_disk.schema_version, head, merged, list(on_disk.rejected))
