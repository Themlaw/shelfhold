"""Import a `shelfhold-export/1` folder into a fresh instance (EF-INT-016).

The one door is `import_all`: it refuses any instance that already holds something the export
carries, so two libraries are never mixed (an import is a move, not a merge). The archive is not
trusted: its manifest, its version, every table and every file are checked against the manifest's
own digests, and a table, a column or a path that this version of the format does not know is a
refusal, never a guess. Nothing is written until everything has been read and checked; the rows go
in one transaction (all or nothing), tried once and taken back before the first file is written, so
a row the schema refuses writes no file. A file placed before a failure stays: nothing refers to
it, and `reconcile` will say so.

What comes back is what `export_all` carried. The profiles come without a password: they get one
with `shelfhold admin reset-password`. What the instance rebuilds itself (the registry of copies,
the search index, the exported shelves of a destination) is rebuilt by it, not imported.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Final, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.schema import metadata as schema_metadata
from shelfhold.infrastructure.schema._base import UtcDateTime
from shelfhold.platform.export_all import DROPPED_COLUMNS, EXPORTED_TABLES, FORMAT, MANIFEST

__all__ = ["ImportRefusedError", "ImportReport", "ImportTarget", "import_all"]

MAX_JSON_BYTES: Final = 256 * 1024 * 1024  # one table, in memory; a larger library is a bug here
_INSERT_CHUNK: Final = 500


class ImportRefusedError(Exception):
    """The import did not start or did not complete; `reason` is one short word, `where` names a
    table or a file (never a value: a row of the archive can hold a title or a name)."""

    def __init__(self, reason: str, where: str = "") -> None:
        super().__init__(f"{reason}: {where}" if where else reason)
        self.reason = reason
        self.where = where


class ImportTarget(Protocol):
    """Where the files go in this instance, through its file-system layer."""

    def local_path(self, location: str, root: str, rel_path: str) -> str | None:
        """The absolute local path of this file in this instance, `None` when it has no root."""
        ...

    def writer(self) -> AbstractContextManager[Callable[[str, BinaryIO, str], None]]:
        """`write(local_path, stream, sha256_hex)`: placed atomically, never over a file."""
        ...


@dataclass
class ImportReport:
    rows: dict[str, int]
    files: int
    bytes: int

    def as_dict(self) -> dict[str, Any]:
        return {"rows": sum(self.rows.values()), "tables": len(self.rows), "files": self.files,
                "bytes": self.bytes}  # fmt: skip


def _read_json(path: Path, digest: str, where: str) -> Any:
    try:
        size = path.stat().st_size
        if size > MAX_JSON_BYTES:
            raise ImportRefusedError("too_large", where)
        data = path.read_bytes()
    except OSError as exc:
        raise ImportRefusedError("unreadable", where) from exc
    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise ImportRefusedError("digest_mismatch", where)
    try:
        return json.loads(data)
    except ValueError as exc:
        raise ImportRefusedError("not_json", where) from exc


def _safe_rel(value: object, where: str) -> str:
    if not isinstance(value, str) or not value:
        raise ImportRefusedError("bad_path", where)
    pure = PurePosixPath(value)
    if pure.is_absolute() or ".." in pure.parts or "." in pure.parts or "\\" in value:
        raise ImportRefusedError("bad_path", where)
    return value


@dataclass(frozen=True)
class _File:
    source: Path
    local: str
    sha256: str
    size: int


def _manifest(source: Path) -> dict[str, Any]:
    try:
        raw = (source / MANIFEST).read_bytes()
    except OSError as exc:
        raise ImportRefusedError("no_manifest", MANIFEST) from exc  # also a half-written export
    try:
        manifest = json.loads(raw)
    except ValueError as exc:
        raise ImportRefusedError("not_json", MANIFEST) from exc
    if not isinstance(manifest, dict):
        raise ImportRefusedError("not_json", MANIFEST)
    if manifest.get("format") != FORMAT or manifest.get("schema_version") != FORMAT:
        raise ImportRefusedError("incompatible_version", str(manifest.get("format"))[:40])
    return manifest


def _parse(column: sa.Column[Any], value: Any) -> Any:
    if value is not None and isinstance(column.type, (sa.DateTime, UtcDateTime)):
        if not isinstance(value, str):
            raise ImportRefusedError("bad_value", column.table.name)
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ImportRefusedError("bad_value", column.table.name) from exc
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return value


def _load_tables(source: Path, manifest: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    declared = manifest.get("tables")
    if not isinstance(declared, dict):
        raise ImportRefusedError("bad_manifest", "tables")
    loaded: dict[str, list[dict[str, Any]]] = {}
    for name, meta in declared.items():
        table = schema_metadata.tables.get(name)
        if table is None or name not in EXPORTED_TABLES or not isinstance(meta, dict):
            raise ImportRefusedError("unknown_table", str(name)[:60])
        rows = _read_json(source / "data" / f"{name}.json", str(meta.get("sha256")), name)
        if not isinstance(rows, list) or len(rows) != meta.get("rows"):
            raise ImportRefusedError("bad_table", name)
        allowed = {c.name for c in table.columns} - DROPPED_COLUMNS.get(name, frozenset())
        converted = []
        for row in rows:
            if not isinstance(row, dict) or not set(row) <= allowed:
                raise ImportRefusedError("unknown_column", name)  # `password_hash` lands here
            converted.append({k: _parse(table.c[k], v) for k, v in row.items()})
        loaded[name] = converted
    return loaded


def _plan_files(source: Path, manifest: Mapping[str, Any], target: ImportTarget) -> list[_File]:
    entries = manifest.get("files")
    if not isinstance(entries, list):
        raise ImportRefusedError("bad_manifest", "files")
    planned: list[_File] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ImportRefusedError("bad_manifest", "files")
        rel = _safe_rel(entry.get("file"), "files")
        inner = _safe_rel(entry.get("path"), "files")
        location, root = entry.get("location"), entry.get("root")
        if location not in ("library", "store") or not isinstance(root, str):
            raise ImportRefusedError("bad_manifest", "files")
        digest, size = entry.get("sha256"), entry.get("size")
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            raise ImportRefusedError("bad_manifest", "files")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ImportRefusedError("bad_manifest", "files")
        local = target.local_path(location, root, inner)
        if local is None:
            raise ImportRefusedError("no_root", f"{location}:{root}")
        if local in seen:
            raise ImportRefusedError("bad_manifest", "files")
        seen.add(local)
        file = source / PurePosixPath(rel)
        if file.is_symlink() or not file.is_file() or file.stat().st_size != size:
            raise ImportRefusedError("missing_file", rel)
        planned.append(_File(file, local, digest.removeprefix("sha256:"), size))
    return planned


def _is_empty(connection: Connection) -> bool:
    for table in schema_metadata.sorted_tables:
        if (
            table.name in EXPORTED_TABLES
            and connection.execute(sa.select(sa.literal(1)).select_from(table).limit(1)).first()
        ):
            return False
    return True


def _chunks(rows: list[dict[str, Any]]) -> Iterator[list[dict[str, Any]]]:
    """Rows by identical set of columns, a few hundred at a time (one statement shape each)."""
    shapes: dict[frozenset[str], list[dict[str, Any]]] = {}
    for row in rows:
        shapes.setdefault(frozenset(row), []).append(row)
    for group in shapes.values():
        for start in range(0, len(group), _INSERT_CHUNK):
            yield group[start : start + _INSERT_CHUNK]


def _insert(connection: Connection, tables: Mapping[str, list[dict[str, Any]]]) -> None:
    connection.exec_driver_sql("PRAGMA defer_foreign_keys=ON")  # checked once, at the commit
    for table in schema_metadata.sorted_tables:
        rows = tables.get(table.name)
        if not rows:
            continue
        try:
            for chunk in _chunks(rows):
                connection.execute(sa.insert(table), chunk)
        except sa.exc.SQLAlchemyError as exc:  # no value in the message: it could be a title
            raise ImportRefusedError("rows_rejected", table.name) from exc
    broken = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
    if broken:
        raise ImportRefusedError("dangling_reference", str(broken[0][0]))


def import_all(
    engine: Engine,
    source: Path | str,
    *,
    target: ImportTarget,
    before_commit: Callable[[Connection, ImportReport], None] | None = None,
) -> ImportReport:
    """Import `source` into `engine`'s database and the files into `target`.

    `before_commit` runs inside the transaction of the rows (the audit entry goes there: the
    rows and the record of who brought them in stand or fall together)."""
    folder = Path(source)
    manifest = _manifest(folder)
    tables = _load_tables(folder, manifest)
    files = _plan_files(folder, manifest, target)
    counts = {n: len(r) for n, r in tables.items()}
    report = ImportReport(counts, len(files), sum(f.size for f in files))
    _load(engine, tables, report, commit=False)  # a dry run: what the schema refuses, refused now
    if files:  # an export with no file needs no root at all
        with target.writer() as write:
            for file in files:
                with file.source.open("rb") as stream:
                    write(file.local, stream, file.sha256)
    _load(engine, tables, report, commit=True, before_commit=before_commit)
    return report


def _load(
    engine: Engine,
    tables: Mapping[str, list[dict[str, Any]]],
    report: ImportReport,
    *,
    commit: bool,
    before_commit: Callable[[Connection, ImportReport], None] | None = None,
) -> None:
    """All the rows in one transaction that takes the write lock first. Without `commit` they go
    in and are taken back: the same refusals, nothing kept."""
    with engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE").connect() as connection:
        transaction = connection.begin()
        try:
            if not _is_empty(connection):  # also what another writer did since the last look
                raise ImportRefusedError("not_empty")
            _insert(connection, tables)
            if commit and before_commit is not None:
                before_commit(connection, report)
        except BaseException:
            transaction.rollback()
            raise
        if commit:
            transaction.commit()
        else:
            transaction.rollback()
