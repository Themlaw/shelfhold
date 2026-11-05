"""`unit.json`: a regenerable projection of the database, in `/data/meta` (ENF-OPS-020, EF-BIB-006).

The database is the authority. The file is written after the transaction that changes the Unit
has committed, and repaired when it diverges (`repair`, at boot and by `shelfhold meta rebuild`).
Its place is flat and stable, by identifier alone (`meta/units/<ulid[0:2]>/<ulid>.json`), so no
merge or split of Works ever moves it. It lives in `/data` in every mode, never in /library or
/store. A file of a newer format is left alone and reported (ENF-OPS-013); a corrupt one is
replaced. Writing goes through `infrastructure.volumes.write_data_file` (atomic, owner-only).
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.infrastructure.volumes import read_data_file, write_data_file
from shelfhold.library.domain.unit_json import (
    MAX_BYTES,
    NewerFormatError,
    UnitJson,
    UnitJsonCorruptError,
)
from shelfhold.library.replica import manifest_file_hashes

META_DIR: Final = "meta/units"
Outcome = Literal["written", "unchanged", "read_only", "missing_unit"]

_CONTENT_FIELDS: Final = (
    "id", "variant_id", "media_type", "format", "retention", "path", "store_provenance",
    "original_sha256", "content_hash", "content_hash_by", "derived_hash", "derived_by",
    "size", "page_count",
)  # fmt: skip


def meta_rel(unit_id: str) -> str:
    if not is_ulid(unit_id):
        raise ValueError(f"not an identifier: {unit_id!r}")
    return f"{META_DIR}/{unit_id[:2]}/{unit_id}.json"


def meta_path(data_dir: Path, unit_id: str) -> Path:
    return data_dir / meta_rel(unit_id)


@dataclass
class RepairReport:
    written: list[str] = field(default_factory=list)
    read_only: list[str] = field(default_factory=list)  # newer format: `reconcile.drift`


def unit_fields(connection: Connection, unit_id: str) -> dict[str, Any] | None:
    unit = connection.execute(
        sa.select(catalog.unit.c.id, catalog.unit.c.edition_id).where(catalog.unit.c.id == unit_id)
    ).first()
    if unit is None:
        return None
    contents = connection.execute(
        sa.select(*(storage.content.c[name] for name in _CONTENT_FIELDS))
        .where(storage.content.c.unit_id == unit_id)
        .order_by(storage.content.c.id)
    ).mappings()
    return {
        "unit_id": unit.id,
        "edition_id": unit.edition_id,
        "contents": [
            {("content_id" if k == "id" else k): v for k, v in row.items()} for row in contents
        ],
        "file_hashes": manifest_file_hashes(connection, unit_id),
    }


class MetaProjection:
    def __init__(self, engine: Engine, data_dir: Path) -> None:
        self._engine = engine
        self._data_dir = data_dir

    def dispose(self) -> None:
        self._engine.dispose()

    def project(self, unit_id: str, *, dry_run: bool = False) -> Outcome:
        with self._engine.connect() as connection:
            known = unit_fields(connection, unit_id)
        if known is None:
            return "missing_unit"
        path = meta_path(self._data_dir, unit_id)
        wanted: UnitJson
        try:
            raw = read_data_file(path, MAX_BYTES + 1)
        except FileNotFoundError:
            wanted = UnitJson.new(known)
        else:
            try:
                wanted = UnitJson.parse(raw).merge(known)
            except NewerFormatError:
                return "read_only"
            except UnitJsonCorruptError:
                wanted = UnitJson.new(known)  # unreadable: the database says what it holds
            else:
                if wanted.dumps() == raw:
                    return "unchanged"
        if not dry_run:
            write_data_file(self._data_dir, meta_rel(unit_id), wanted.dumps())
        return "written"

    def repair(self, *, dry_run: bool = False) -> RepairReport:
        """Every Unit: write what is missing, torn or drifted. Idempotent. With `dry_run`, only
        say which ones would be written."""
        report = RepairReport()
        with self._engine.connect() as connection:
            unit_ids = list(
                connection.execute(
                    sa.select(catalog.unit.c.id).order_by(catalog.unit.c.id)
                ).scalars()
            )
        for unit_id in unit_ids:
            outcome = self.project(unit_id, dry_run=dry_run)
            if outcome == "written":
                report.written.append(unit_id)
            elif outcome == "read_only":
                report.read_only.append(unit_id)
        return report
