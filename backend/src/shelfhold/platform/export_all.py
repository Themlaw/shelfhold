"""The full open export `shelfhold-export/1` (EF-INT-016, EF-BIB-053 (5), EF-INT-027).

A folder of versioned JSON (one file per table) and the file tree of the managed files, written
for another program or another instance to read. What it never carries:

- **a tombstone**, nor anything that would name what one covers: no tombstone table, and no Unit,
  Variant, file, Work or upstream series that a tombstone covers, read from the tombstones
  themselves (a purge that stopped before its rows went leaves them in the database; they stay
  out);
- **a secret**: no key, password hash, token, session, passkey, passphrase or encrypted value;
- **a destination**: targets, their credentials and the paths of the host stay behind.

The tables are listed, not discovered: a table is exported only because this module says so, and
`EXCLUDED_TABLES` gives the reason for every other one (a test holds the two lists to the schema, so
a table added later is decided, never carried by accident).
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Final, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.public import TombstoneScreen, tombstoned_work_ids
from shelfhold.export.application.blocked_units import UnitScreen, blocked_units
from shelfhold.infrastructure.schema import acquisition, catalog, export, storage
from shelfhold.infrastructure.schema import metadata as schema_metadata
from shelfhold.library.public import unit_files
from shelfhold.platform.export_tree import ExportTree

__all__ = [
    "DROPPED_COLUMNS",
    "EXCLUDED_TABLES",
    "EXPORTED_TABLES",
    "FORMAT",
    "ExportReport",
    "ExportScreen",
    "WorkScreen",
    "export_all",
    "file_label",
]

FORMAT: Final = "shelfhold-export/1"
MANIFEST: Final = "manifest.json"

# What is carried, with the reason it is wanted (EF-INT-016: Works, Editions, shelves, the state of
# each profile, requests, files). Parents come before children in the schema's own order.
EXPORTED_TABLES: Final = frozenset(
    {
        "series", "series_member", "work", "work_title", "contributor", "contributor_name",
        "contribution", "external_id", "metadata_field", "edition", "volume", "team",
        "team_priority", "unit", "source_item", "source_binding", "unit_variant",
        "variant_revision", "content", "export_item", "unit_coverage", "series_follow",
        "shelf", "user", "user_state", "reading_position", "request", "profile_source_policy",
    }
)  # fmt: skip

_CREDENTIAL = "a secret, a credential or what opens one"
_SEEDED = "a closed list the code seeds"
_TOMBSTONE = "a tombstone or what is written around one (EF-BIB-053)"
_REBUILT = "rebuilt by the instance itself"
_HOST = "the host's own setup: paths, targets, roots, schedules"
_NOISE = "a queue, a journal, a cache or a log: it says what happened, not what is held"

# Every other table, and why it stays behind. Keep it a plain table: the test reads it.
EXCLUDED_TABLES: Final[Mapping[str, str]] = {
    # secrets
    "api_key": _CREDENTIAL, "webauthn_credential": _CREDENTIAL, "session": _CREDENTIAL,
    "secret_key": _CREDENTIAL, "export_target": _CREDENTIAL, "source_account": _CREDENTIAL,
    "source_secret": _CREDENTIAL, "source_config": _CREDENTIAL, "plugin_attestation": _CREDENTIAL,
    "idempotency_key": _CREDENTIAL,
    # tombstones
    "content_tombstone": _TOMBSTONE, "tombstone_kid": _TOMBSTONE,
    "tombstone_lost_kid": _TOMBSTONE, "tombstone_pending_root": _TOMBSTONE,
    # the host's setup
    "physical_root": _HOST, "mount_sentinel": _HOST, "instance": _HOST, "app_setting": _HOST,
    "schedule": _HOST, "time_window": _HOST, "plugin_install": _HOST, "plugin_state": _HOST,
    "shelf_destination": _HOST, "shelf_destination_remote_library": _HOST,
    "remote_library": _HOST, "kavita_instance": _HOST, "kavita_access_audit": _HOST,
    "sub_source": _HOST, "backup_run": _HOST,
    "work_kind": _SEEDED, "work_relation_kind": _SEEDED, "normalizer": _SEEDED,
    # queues, journals, caches, logs
    "audit": _NOISE, "event": _NOISE, "job_view": _NOISE, "fs_intent": _NOISE,
    "resource_lock": _NOISE, "export_plan": _NOISE, "export_op": _NOISE,
    "notification_pending": _NOISE, "notification_digest": _NOISE, "domain_slot": _NOISE,
    "provider_quota": _NOISE, "http_cache": _NOISE, "metadata_cache": _NOISE,
    "follow_notice": _NOISE, "follow_request": _NOISE, "download_request": _NOISE,
    "download_request_unit": _NOISE, "source_failure": _NOISE, "series_gap": _NOISE,
    "gap_ack": _NOISE, "binding_release_sample": _NOISE, "revision_pending": _NOISE,
    "merge_log": _NOISE, "merge_suggestion": _NOISE, "age_suggestion": _NOISE,
    "edition_attach": _NOISE, "source_alias": _NOISE, "source_alias_migration": _NOISE,
    "pending_source_item": _NOISE, "upstream_snapshot": _NOISE, "trash_item": _NOISE,
    "safeguard_backlog": _NOISE, "import_entry": _NOISE, "work_redirect": _NOISE,
    # rebuilt by the instance
    "replica": _REBUILT, "thumbnail": _REBUILT, "team_alias": _REBUILT,
    "export_record": _REBUILT, "export_series": _REBUILT, "edition_export_series": _REBUILT,
    "export_upgrade": _REBUILT, "field_owner": _REBUILT, "document_alias": _REBUILT,
    "work_relation": _REBUILT,
}  # fmt: skip

# Columns of an exported table that never leave: the password hash is the only secret in one.
DROPPED_COLUMNS: Final[Mapping[str, frozenset[str]]] = {"user": frozenset({"password_hash"})}

_CHUNK: Final = 500  # identifiers per query: SQLite caps the parameters of one statement
_LIBRARY: Final = "library"
_STORE: Final = "store"
_UNTRUSTED: Final = (
    "untrusted/"  # the originals of untrusted EPUBs are handed to nothing (EF-BIB-051)
)


class ExportScreen(UnitScreen, Protocol):
    """The tombstone screen: the three questions of a Unit's rows, and a file's content alone."""

    def content_blocked(
        self, root_id: str | None, content_hashes: Sequence[tuple[str, str]]
    ) -> bool: ...


WorkScreen = TombstoneScreen  # the Work tombstones, in plain values (the catalog's own screen)


FileOpener = Callable[[str], AbstractContextManager[BinaryIO]]


@dataclass
class ExportReport:
    rows: dict[str, int] = field(default_factory=dict)
    files: int = 0
    bytes: int = 0
    withheld_units: int = 0
    withheld_works: int = 0
    files_missing: int = 0
    files_off_registry: int = 0  # copied, but not the bytes the registry says (a damaged copy)

    def as_dict(self) -> dict[str, Any]:
        return {
            "rows": sum(self.rows.values()),
            "tables": len(self.rows),
            "files": self.files,
            "bytes": self.bytes,
            "withheld_units": self.withheld_units,
            "withheld_works": self.withheld_works,
            "files_missing": self.files_missing,
            "files_off_registry": self.files_off_registry,
        }


def file_label(root_path: str, library_dir: Path | str) -> str:
    """How a library root is named in the tree: its path below `library_dir`, so that the instance
    that imports it finds the same root under its own `library_dir` (`manga` for `/library/manga`).
    A root outside `library_dir` has no portable name: its last folder stands for it."""
    path, base = Path(root_path), Path(library_dir)
    try:
        return path.relative_to(base).as_posix() or "."
    except ValueError:
        return path.name


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=UTC)).isoformat()
    return value


def _dump(rows: Iterable[Mapping[str, Any]]) -> bytes:
    return (json.dumps(list(rows), ensure_ascii=False, sort_keys=True, indent=1) + "\n").encode()


@dataclass
class _Kept:
    """The identifiers that stay, worked out once before a single row is written."""

    works: set[str]
    editions: set[str]
    units: set[str]
    series: set[str] = field(default_factory=set)
    volumes: set[str] = field(default_factory=set)
    contributors: set[str] = field(default_factory=set)
    items: set[str] = field(default_factory=set)
    bindings: set[str] = field(default_factory=set)
    variants: set[str] = field(default_factory=set)
    contents: set[str] = field(default_factory=set)
    export_items: set[str] = field(default_factory=set)
    withheld_units: int = 0
    withheld_works: int = 0

    @property
    def entities(self) -> set[str]:
        return (
            self.works | self.editions | self.series | self.volumes | self.contributors
        )  # identifiers are ULIDs: one set holds every entity type


def _ids(connection: Connection, column: sa.Column[Any]) -> set[str]:
    return {str(v) for v in connection.execute(sa.select(column)).scalars()}


def _decide(connection: Connection, screen: ExportScreen, works: WorkScreen) -> _Kept:
    """What the tombstones cover, then what hangs from what stays. Nothing here writes."""
    covered = blocked_units(connection, screen)
    edition_work = {
        str(r.id): str(r.work_id)
        for r in connection.execute(sa.select(catalog.edition.c.id, catalog.edition.c.work_id))
    }
    unit_edition = {
        str(r.id): str(r.edition_id)
        for r in connection.execute(sa.select(catalog.unit.c.id, catalog.unit.c.edition_id))
    }
    all_works = _ids(connection, catalog.work.c.id)
    out_works = set(tombstoned_work_ids(connection, works, sorted(all_works)))  # `work` scope
    units_of: dict[str, list[str]] = defaultdict(list)
    for unit_id, edition_id in unit_edition.items():
        units_of[edition_work[edition_id]].append(unit_id)
    for work_id, unit_ids in units_of.items():
        if unit_ids and all(u in covered for u in unit_ids):
            out_works.add(work_id)  # nothing of it is left to carry but its name
    kept = _Kept(
        works=all_works - out_works,
        editions={e for e, w in edition_work.items() if w not in out_works},
        units=set(),
    )
    kept.units = {u for u, e in unit_edition.items() if e in kept.editions and u not in covered}
    kept.withheld_units = len(unit_edition) - len(kept.units)
    kept.withheld_works = len(out_works)
    _decide_sources(connection, screen, kept)
    _decide_files(connection, screen, kept)
    _decide_catalog(connection, kept)
    return kept


def _decide_sources(connection: Connection, screen: ExportScreen, kept: _Kept) -> None:
    item, binding, variant = (
        acquisition.source_item,
        acquisition.source_binding,
        catalog.unit_variant,
    )
    barred = {
        str(r.id)
        for r in connection.execute(
            sa.select(item.c.id, item.c.plugin_id, item.c.source_key, item.c.remote_id)
        )
        if screen.series_blocked(None, r.plugin_id, r.source_key, r.remote_id)
    }  # the upstream series a `series_binding` tombstone names: its memo may spell the title
    kept.bindings = {
        str(r.id)
        for r in connection.execute(
            sa.select(binding.c.id, binding.c.edition_id, binding.c.source_item_id)
        )
        if r.edition_id in kept.editions and r.source_item_id not in barred
    }
    kept.variants = {
        str(r.id)
        for r in connection.execute(
            sa.select(variant.c.id, variant.c.unit_id, variant.c.source_binding_id)
        )
        if r.unit_id in kept.units and r.source_binding_id in kept.bindings
    }
    used = {
        str(r.source_item_id)
        for r in connection.execute(sa.select(binding.c.id, binding.c.source_item_id))
        if r.id in kept.bindings
    }
    kept.items = used | {
        str(r.source_item_id)
        for r in connection.execute(sa.select(variant.c.id, variant.c.source_item_id))
        if r.id in kept.variants and r.source_item_id is not None
    }


def _hashes(row: Any) -> list[tuple[str, str]]:
    found = []
    if row.content_hash and row.content_hash_by:
        found.append((row.content_hash_by, row.content_hash))
    if row.derived_hash and row.derived_hash_by:
        found.append((row.derived_hash_by, row.derived_hash))
    return found


def _decide_files(connection: Connection, screen: ExportScreen, kept: _Kept) -> None:
    content = storage.content
    for row in connection.execute(sa.select(content)):
        if row.unit_id is not None and row.unit_id not in kept.units:
            continue
        if row.variant_id is not None and row.variant_id not in kept.variants:
            continue
        hashes = _hashes(row)
        if hashes and screen.content_blocked(None, hashes):
            continue  # the very bytes a tombstone names, filed under no Unit (an adopted file)
        kept.contents.add(str(row.id))
    coverage = export.unit_coverage
    carried: dict[str, set[str]] = defaultdict(set)
    for r in connection.execute(
        sa.select(coverage.c.carrier_id, coverage.c.unit_id).where(
            coverage.c.carrier_type == "export_item"
        )
    ):
        carried[str(r.carrier_id)].add(str(r.unit_id))
    items = export.export_item
    kept.export_items = {
        str(r.id)
        for r in connection.execute(sa.select(items.c.id, items.c.edition_id, items.c.content_id))
        if r.edition_id in kept.editions
        and (r.content_id is None or r.content_id in kept.contents)
        and carried[str(r.id)] <= kept.units  # a file that also holds a covered chapter stays out
    }


def _decide_catalog(connection: Connection, kept: _Kept) -> None:
    member = catalog.series_member
    kept.series = {
        str(r.series_id)
        for r in connection.execute(sa.select(member.c.series_id, member.c.work_id))
        if r.work_id in kept.works
    }  # a series with no Work left is not a thing a purge leaves (it removes it too)
    kept.volumes = {
        str(r.id)
        for r in connection.execute(sa.select(catalog.volume.c.id, catalog.volume.c.edition_id))
        if r.edition_id in kept.editions
    }
    credit = catalog.contribution
    credited: dict[str, set[str]] = defaultdict(set)
    for r in connection.execute(sa.select(credit.c.contributor_id, credit.c.entity_id)):
        credited[str(r.contributor_id)].add(str(r.entity_id))
    named = {
        str(r.text_version_contributor_id)
        for r in connection.execute(
            sa.select(catalog.edition.c.id, catalog.edition.c.text_version_contributor_id)
        )
        if r.id in kept.editions and r.text_version_contributor_id is not None
    }
    everyone = _ids(connection, catalog.contributor.c.id)
    entities = kept.works | kept.editions | kept.series | kept.volumes
    kept.contributors = {
        c for c in everyone if c in named or not credited[c] or credited[c] & entities
    }  # an author only credited on purged Works goes with them


def _keep(table: str, row: Mapping[str, Any], kept: _Kept) -> bool:
    match table:
        case "series":
            return str(row["id"]) in kept.series
        case "series_member":
            return row["series_id"] in kept.series and row["work_id"] in kept.works
        case "work":
            return row["id"] in kept.works
        case "work_title" | "user_state":
            return row["work_id"] in kept.works
        case "contributor":
            return row["id"] in kept.contributors
        case "contributor_name":
            return row["contributor_id"] in kept.contributors
        case "contribution":
            return row["contributor_id"] in kept.contributors and row["entity_id"] in kept.entities
        case "external_id" | "metadata_field":
            return row["entity_id"] in kept.entities
        case "edition":
            return row["id"] in kept.editions
        case "volume":
            return row["id"] in kept.volumes
        case "team" | "series_follow":
            return row["edition_id"] in kept.editions
        case "team_priority":
            return row["edition_id"] in kept.editions
        case "unit":
            return row["id"] in kept.units
        case "source_item":
            return row["id"] in kept.items
        case "source_binding":
            return row["id"] in kept.bindings
        case "unit_variant":
            return row["id"] in kept.variants
        case "variant_revision":
            return row["variant_id"] in kept.variants
        case "content":
            return row["id"] in kept.contents
        case "export_item":
            return row["id"] in kept.export_items
        case "unit_coverage":
            if row["unit_id"] not in kept.units:
                return False
            carrier = {
                "export_item": kept.export_items,
                "content": kept.contents,
                "unit": kept.units,
                "volume": kept.volumes,
            }.get(row["carrier_type"])
            return carrier is not None and row["carrier_id"] in carrier
        case "reading_position":
            carrier = {"export_item": kept.export_items, "content": kept.contents}.get(
                row["carrier_type"]
            )
            return carrier is not None and row["carrier_id"] in carrier
        case _:  # shelf, user, request, profile_source_policy: no tombstone reaches them
            return True


def _repair(table: str, row: dict[str, Any], kept: _Kept) -> dict[str, Any]:
    """A reference to something left out is cut, never exported dangling: another Edition this one
    was translated from, a Unit that hid a chapter, the Work that was purged."""
    if table == "edition" and row["translated_from_edition_id"] not in (None, *kept.editions):
        row["translated_from_edition_id"] = None
    if table == "edition" and row["text_version_contributor_id"] not in (None, *kept.contributors):
        row["text_version_contributor_id"] = None
    return row


def _rows(connection: Connection, table: sa.Table, kept: _Kept) -> list[dict[str, Any]]:
    dropped = DROPPED_COLUMNS.get(table.name, frozenset())
    names = [c.name for c in table.columns if c.name not in dropped]
    out: list[dict[str, Any]] = []
    for row in connection.execute(sa.select(*(table.c[n] for n in names))):
        item = dict(row._mapping)
        if _keep(table.name, item, kept):
            out.append(_repair(table.name, {k: _jsonable(v) for k, v in item.items()}, kept))
    return out


def _tables() -> list[sa.Table]:
    return [t for t in schema_metadata.sorted_tables if t.name in EXPORTED_TABLES]


def _copy_files(
    connection: Connection,
    kept: _Kept,
    tree: ExportTree,
    opener: FileOpener,
    library_dir: Path | str,
    report: ExportReport,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    entries: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    units = sorted(kept.units)
    copies = [
        c
        for start in range(0, len(units), _CHUNK)
        for c in unit_files(connection, units[start : start + _CHUNK])
        if c.location in (_LIBRARY, _STORE) and not c.rel_path.startswith(_UNTRUSTED)
    ]
    done: set[tuple[str, str]] = set()
    for copy in sorted(copies, key=lambda c: (c.location, c.root_path, c.rel_path)):
        root = _STORE if copy.location == _STORE else file_label(copy.root_path, library_dir)
        if (root, copy.rel_path) in done:
            continue
        done.add((root, copy.rel_path))
        below = [] if root in (_STORE, ".") else [root]
        rel = "/".join(("files", copy.location, *below, copy.rel_path))
        try:
            with opener(f"{copy.root_path.rstrip('/')}/{copy.rel_path}") as source:
                digest, size = tree.put_stream(rel, source)
        except FileNotFoundError:  # the registry knows a file the disk lost: said, not hidden
            report.files_missing += 1
            missing.append(
                {"unit_id": copy.unit_id, "location": copy.location, "path": copy.rel_path}
            )
            continue
        off = copy.file_hash is not None and copy.file_hash != digest
        report.files += 1
        report.bytes += size
        report.files_off_registry += int(off)
        entries.append(
            {
                "unit_id": copy.unit_id, "location": copy.location, "root": root,
                "path": copy.rel_path, "file": rel, "size": size, "sha256": digest,
                "matches_registry": None if copy.file_hash is None else not off,
            }
        )  # fmt: skip
    return entries, missing


def export_all(
    connection: Connection,
    *,
    tree: ExportTree,
    screen: ExportScreen,
    works: WorkScreen,
    opener: FileOpener,
    library_dir: Path | str,
    now: datetime,
    only: Collection[str] | None = None,
) -> ExportReport:
    """Write the export into `tree` from one consistent read of the database (`connection` is a
    transaction that only reads). `manifest.json` goes last: a folder without one is not an export.
    `only` narrows the tables, for a test."""
    report = ExportReport()
    kept = _decide(connection, screen, works)
    report.withheld_units, report.withheld_works = kept.withheld_units, kept.withheld_works
    tables: dict[str, dict[str, Any]] = {}
    for table in _tables():
        if only is not None and table.name not in only:
            continue
        rows = _rows(connection, table, kept)
        data = _dump(rows)
        tree.put_bytes(f"data/{table.name}.json", data)
        report.rows[table.name] = len(rows)
        tables[table.name] = {"rows": len(rows), "sha256": _sha(data)}
    entries, missing = _copy_files(connection, kept, tree, opener, library_dir, report)
    manifest = {
        "format": FORMAT,
        "schema_version": FORMAT,
        "created_at": now.astimezone(UTC).isoformat(),
        "tables": tables,
        "files": entries,
        "files_missing": missing,
        "excluded_tables": sorted(EXCLUDED_TABLES),
    }
    tree.put_bytes(MANIFEST, (json.dumps(manifest, sort_keys=True, indent=1) + "\n").encode())
    return report


def _sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()
