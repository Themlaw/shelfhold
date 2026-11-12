"""Full open export ``shelfhold-export/1`` (EF-INT-016, EF-BIB-053 (5), EF-INT-027).

Writes a versioned, documented JSON export and a file tree. Content under a tombstone is excluded
and no tombstone that would name a title is carried.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.schema import metadata as schema_metadata

SCHEMA_VERSION = "shelfhold-export/1"

# Tables never exported: tombstones (and their registries), secrets, ephemeral/session state,
# jobs/intents/locks/plans/events/audit noise, caches, and anything that would carry a title of
# purged content through a tombstone. ``content_tombstone`` and friends are excluded explicitly.
_EXCLUDED_TABLES: frozenset[str] = frozenset(
    {
        "content_tombstone",
        "tombstone_kid",
        "tombstone_lost_kid",
        "tombstone_pending_root",
        "secret_key",
        "session",
        "api_key",
        "webauthn_credential",
        "idempotency_key",
        "fs_intent",
        "resource_lock",
        "export_plan",
        "export_op",
        "job",
        "job_dead_letter",
        "event",
        "notification_pending",
        "audit",
        "safeguard_backlog",
        "import_entry",
        "thumbnail",
        "migration_state",
        "alembic_version",
    }
)

# Columns dropped from exported rows (secrets / hashes / local-only material).
_DROP_COLUMNS: dict[str, frozenset[str]] = {
    "user": frozenset({"password_hash"}),
    "export_target": frozenset({"secret_nonce", "secret_ciphertext", "secret_kid"}),
}


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value).hex()
    return value


def _row_to_dict(row: sa.Row[Any], table: sa.Table) -> dict[str, Any]:
    dropped = _DROP_COLUMNS.get(table.name, frozenset())
    return {
        col.name: _jsonable(row._mapping[col.name])
        for col in table.columns
        if col.name not in dropped
    }


def _tables_in_order() -> list[sa.Table]:
    ordered = [t for t in schema_metadata.sorted_tables if t.name not in _EXCLUDED_TABLES]
    return ordered


def _clear_dir(path: Path) -> None:
    for child in sorted(path.rglob("*"), reverse=True):
        if child.is_file() or child.is_symlink():
            child.unlink()
        elif child.is_dir():
            child.rmdir()
    if path.is_dir():
        path.rmdir()


def _copy_file(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_file():
        dst.write_bytes(src.read_bytes())


def _export_files(
    connection: sa.Connection,
    output: Path,
    roots: dict[str, Path],
) -> list[dict[str, Any]]:
    """Copy kept content files into the export tree and return their manifest entries."""
    entries: list[dict[str, Any]] = []
    try:
        content = schema_metadata.tables["content"]
    except KeyError:
        return entries
    rows = connection.execute(sa.select(content)).all()
    files_dir = output / "files"
    for row in rows:
        mapping = row._mapping
        path = mapping.get("path")
        retention = mapping.get("retention")
        record: dict[str, Any] = {
            "content_id": _jsonable(mapping.get("id")),
            "retention": retention,
            "sha256": _jsonable(mapping.get("original_sha256")),
        }
        copied = None
        if retention == "keep" and path:
            # kept files live under /store/<path>
            src = roots.get("store", Path()) / str(path)
            if src.is_file():
                rel = Path("files/store") / str(path)
                _copy_file(src, files_dir / "store" / str(path))
                copied = rel.as_posix()
        elif retention in {"passthrough", "adopted_ref"} and path:
            # best effort: try library then store roots
            for name in ("library", "store"):
                src = roots.get(name, Path()) / str(path)
                if src.is_file():
                    _copy_file(src, files_dir / name / str(path))
                    copied = (Path("files") / name / str(path)).as_posix()
                    break
        if copied:
            record["file"] = copied
        entries.append(record)
    return entries


def export_all(
    engine: Engine,
    data_dir: Path | str,
    library_dir: Path | str,
    store_dir: Path | str,
    output: Path | str,
) -> Path:
    """Export the instance into ``output`` as ``shelfhold-export/1``.

    The signature accepts the engine and the data/library/store roots (as in the integration
    environment). ``output`` is created or replaced.
    """
    output = Path(output)
    data_dir, library_dir, store_dir = Path(data_dir), Path(library_dir), Path(store_dir)
    if output.exists():
        _clear_dir(output)
    (output / "data").mkdir(parents=True, exist_ok=True)

    roots = {"data": data_dir, "library": library_dir, "store": store_dir}
    exported_tables: list[str] = []

    with engine.connect() as connection:
        for table in _tables_in_order():
            try:
                rows = connection.execute(sa.select(table)).all()
            except sa.exc.SQLAlchemyError:
                continue
            payload = [_row_to_dict(r, table) for r in rows]
            (output / "data" / f"{table.name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            exported_tables.append(table.name)

        file_entries = _export_files(connection, output, roots)

    manifest = {
        "schema_version": SCHEMA_VERSION,
        "format": SCHEMA_VERSION,
        "created_at": datetime.now(tz=UTC).isoformat(),
        "tables": exported_tables,
        "files": file_entries,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    return output
