"""Import a ``shelfhold-export/1`` archive into a fresh instance (EF-INT-016).

Refused on a non-empty base. The archive must carry a compatible, versioned schema.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import sqlalchemy as sa

from shelfhold.infrastructure.schema import metadata as schema_metadata
from shelfhold.infrastructure.schema._base import UtcDateTime
from shelfhold.platform.export_all import SCHEMA_VERSION

_COMPATIBLE = {SCHEMA_VERSION}


class NonEmptyInstanceError(ValueError):
    """The target instance is not empty: import would mix data."""


def _parse_value(col: sa.Column[Any], value: Any) -> Any:
    if value is None:
        return None
    t = col.type
    if isinstance(t, (sa.DateTime, UtcDateTime)):
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        return value
    if isinstance(t, sa.LargeBinary):
        if isinstance(value, str):
            return bytes.fromhex(value)
        return value
    if isinstance(t, sa.Boolean):
        return bool(value)
    if isinstance(t, sa.Integer):
        return int(value)
    if isinstance(t, (sa.JSON,)):
        return value
    return value


def _is_empty(connection: sa.Connection) -> bool:
    for table in schema_metadata.sorted_tables:
        try:
            count = connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
        except sa.exc.SQLAlchemyError:
            continue
        if count:
            return False
    return True


def _load_table(connection: sa.Connection, table: sa.Table, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = {c.name: c for c in table.columns}
    converted = []
    for row in rows:
        item: dict[str, Any] = {}
        for name, value in row.items():
            col = cols.get(name)
            if col is None:
                continue
            item[name] = _parse_value(col, value)
        if item:
            converted.append(item)
    if converted:
        connection.execute(sa.insert(table), converted)


def _restore_files(manifest: dict[str, Any], export_dir: Path, roots: dict[str, Path]) -> None:
    for entry in manifest.get("files", []):
        rel = entry.get("file")
        if not rel:
            continue
        src = export_dir / rel
        if not src.is_file():
            continue
        # rel is like files/store/<path> or files/library/<path>
        parts = Path(rel).parts
        if len(parts) < 3 or parts[0] != "files":
            continue
        root_name = parts[1]
        target_root = roots.get(root_name)
        if target_root is None:
            continue
        inner = Path(*parts[2:])
        dst = target_root / inner
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(src.read_bytes())


def import_all(
    db_path: Path | str,
    export_dir: Path | str,
    data_dir: Path | str | None = None,
    library_dir: Path | str | None = None,
    store_dir: Path | str | None = None,
) -> None:
    """Import ``export_dir`` into the database at ``db_path``.

    The database must be absent or empty (fresh instance). Directories for file restoration may be
    provided; when omitted, files recorded in the archive are not restored.
    """
    db_path = Path(db_path)
    export_dir = Path(export_dir)
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("schema_version") or manifest.get("format")
    if version not in _COMPATIBLE:
        raise ValueError(f"incompatible export schema: {version!r}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = sa.create_engine(f"sqlite:///{db_path}")
    try:
        schema_metadata.create_all(engine)
        with engine.begin() as connection:
            if not _is_empty(connection):
                raise NonEmptyInstanceError("target instance is not empty")
            # Disable FK enforcement during bulk load; we restore in dependency order anyway.
            connection.execute(sa.text("PRAGMA foreign_keys=OFF"))
            for table in schema_metadata.sorted_tables:
                data_file = export_dir / "data" / f"{table.name}.json"
                if not data_file.exists():
                    continue
                rows = json.loads(data_file.read_text(encoding="utf-8"))
                _load_table(connection, table, rows)
            connection.execute(sa.text("PRAGMA foreign_keys=ON"))
    finally:
        engine.dispose()

    roots: dict[str, Path] = {}
    if data_dir is not None:
        roots["data"] = Path(data_dir)
    if library_dir is not None:
        roots["library"] = Path(library_dir)
    if store_dir is not None:
        roots["store"] = Path(store_dir)
    if roots:
        _restore_files(manifest, export_dir, roots)
