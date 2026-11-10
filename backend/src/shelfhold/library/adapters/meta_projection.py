"""`unit.json` and `work.json`: regenerable projections of the database, in `/data/meta`
(ENF-OPS-020, ENF-OPS-013, EF-BIB-006).

The database is the authority. The file is written after the transaction that changes the Unit
has committed, and repaired when it diverges (`repair`, at boot and by `shelfhold meta rebuild`).
Its place is flat and stable, by identifier alone (`meta/units/<ulid[0:2]>/<ulid>.json`), so no
merge or split of Works ever moves it. It lives in `/data` in every mode, never in /library or
/store. A file of a newer format is left alone and reported (ENF-OPS-013); a corrupt one is
replaced. `work.json` (`meta/works/<ulid[0:2]>/<ulid>.json`) follows the same rules and holds
nothing but catalog identifiers and titles: no key, no token, no path. Writing goes through
`infrastructure.volumes.write_data_file` (atomic, owner-only).
"""

import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.infrastructure.ids import is_ulid
from shelfhold.infrastructure.schema import catalog, storage
from shelfhold.infrastructure.volumes import read_data_file, write_data_file
from shelfhold.library.adapters.origin_comicinfo import origin_reference
from shelfhold.library.domain.unit_json import (
    MAX_BYTES,
    NewerFormatError,
    UnitJson,
    UnitJsonCorruptError,
)
from shelfhold.library.domain.work_json import WorkJson
from shelfhold.library.replica import manifest_file_hashes

META_DIR: Final = "meta/units"
WORKS_DIR: Final = "meta/works"
Outcome = Literal["written", "unchanged", "read_only", "missing_unit"]

_CONTENT_FIELDS: Final = (
    "id", "variant_id", "media_type", "format", "retention", "path", "store_provenance",
    "original_sha256", "content_hash", "content_hash_by", "derived_hash", "derived_by",
    "size", "page_count",
)  # fmt: skip


_UNIT_EDITION = catalog.unit.join(
    catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id
)


_STRIPES = tuple(threading.Lock() for _ in range(64))


def _guard(identifier: str) -> threading.Lock:
    """One lock per identifier (striped), shared by every projection of this process: the store
    that writes after a commit and the repair at start cannot interleave their read of the
    database and their write of the file, so an older answer never replaces a newer one."""
    return _STRIPES[zlib.crc32(identifier.encode()) % len(_STRIPES)]


def meta_rel(unit_id: str) -> str:
    if not is_ulid(unit_id):
        raise ValueError(f"not an identifier: {unit_id!r}")
    return f"{META_DIR}/{unit_id[:2]}/{unit_id}.json"


def meta_path(data_dir: Path, unit_id: str) -> Path:
    return data_dir / meta_rel(unit_id)


def work_rel(work_id: str) -> str:
    if not is_ulid(work_id):
        raise ValueError(f"not an identifier: {work_id!r}")
    return f"{WORKS_DIR}/{work_id[:2]}/{work_id}.json"


def work_path(data_dir: Path, work_id: str) -> Path:
    return data_dir / work_rel(work_id)


@dataclass
class RepairReport:
    written: list[str] = field(default_factory=list)
    read_only: list[str] = field(default_factory=list)  # newer format: `reconcile.drift`
    works_written: list[str] = field(default_factory=list)
    works_read_only: list[str] = field(default_factory=list)


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


def work_fields(connection: Connection, work_id: str) -> dict[str, Any] | None:
    """What `work.json` says of a Work: catalog identifiers and titles, nothing of the disks."""
    work = connection.execute(
        sa.select(
            catalog.work.c.id, catalog.work.c.kind, catalog.work.c.title,
            catalog.work.c.sort_title, catalog.work.c.status, catalog.work.c.first_published,
            catalog.work.c.date_precision,
        ).where(catalog.work.c.id == work_id)
    ).first()  # fmt: skip
    if work is None:
        return None
    titles = connection.execute(
        sa.select(catalog.work_title.c.title, catalog.work_title.c.lang, catalog.work_title.c.kind)
        .where(catalog.work_title.c.work_id == work_id)
        .order_by(catalog.work_title.c.lang, catalog.work_title.c.kind, catalog.work_title.c.title)
    ).mappings()
    editions = connection.execute(
        sa.select(catalog.edition.c.id).where(catalog.edition.c.work_id == work_id)
        .order_by(catalog.edition.c.id)
    ).scalars()  # fmt: skip
    units = connection.execute(
        sa.select(catalog.unit.c.id)
        .select_from(_UNIT_EDITION)
        .where(catalog.edition.c.work_id == work_id)
        .order_by(catalog.unit.c.id)
    ).scalars()  # fmt: skip
    return {
        "work_id": work.id, "kind": work.kind, "title": work.title, "sort_title": work.sort_title,
        "status": work.status, "first_published": work.first_published,
        "date_precision": work.date_precision, "titles": [dict(t) for t in titles],
        "edition_ids": list(editions), "unit_ids": list(units),
    }  # fmt: skip


class MetaProjection:
    def __init__(self, engine: Engine, data_dir: Path) -> None:
        self._engine = engine
        self._data_dir = data_dir

    def dispose(self) -> None:
        self._engine.dispose()

    def project(self, unit_id: str, *, dry_run: bool = False) -> Outcome:
        with _guard(unit_id):  # the database is read under the lock, just before the write
            outcome = self._project_unit(unit_id, dry_run=dry_run)
        if outcome == "written" and not dry_run:
            self._project_work_of(unit_id)  # the Work of the Unit, after it (its own lock)
        return outcome

    def _project_unit(self, unit_id: str, *, dry_run: bool) -> Outcome:
        with self._engine.connect() as connection:
            known = unit_fields(connection, unit_id)
        if known is None:
            return "missing_unit"
        if (origin := origin_reference(self._data_dir, unit_id)) is not None:
            known["origin_comicinfo"] = origin  # the ComicInfo the service sent (EF-BIB-048)
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

    def _project_work_of(self, unit_id: str) -> None:
        with self._engine.connect() as connection:
            work_id = connection.execute(
                sa.select(catalog.edition.c.work_id)
                .select_from(
                    catalog.unit.join(
                        catalog.edition, catalog.edition.c.id == catalog.unit.c.edition_id
                    )
                )
                .where(catalog.unit.c.id == unit_id)
            ).scalar()
        if work_id is not None:
            self.project_work(str(work_id))

    def project_work(self, work_id: str, *, dry_run: bool = False) -> Outcome:
        """`work.json` of one Work: written when missing, torn or drifted; a newer format is left
        alone."""
        with _guard(work_id):
            return self._project_work(work_id, dry_run=dry_run)

    def _project_work(self, work_id: str, *, dry_run: bool) -> Outcome:
        with self._engine.connect() as connection:
            known = work_fields(connection, work_id)
        if known is None:
            return "missing_unit"
        path = work_path(self._data_dir, work_id)
        wanted: WorkJson
        try:
            raw = read_data_file(path, MAX_BYTES + 1)
        except FileNotFoundError:
            wanted = WorkJson.new(known)
        else:
            try:
                wanted = WorkJson.parse(raw).merge(known)
            except NewerFormatError:
                return "read_only"
            except UnitJsonCorruptError:
                wanted = WorkJson.new(known)
            else:
                if wanted.dumps() == raw:
                    return "unchanged"
        if not dry_run:
            write_data_file(self._data_dir, work_rel(work_id), wanted.dumps())
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
        with self._engine.connect() as connection:
            work_ids = list(
                connection.execute(
                    sa.select(catalog.work.c.id).order_by(catalog.work.c.id)
                ).scalars()
            )
        for work_id in work_ids:
            outcome = self.project_work(work_id, dry_run=dry_run)
            if outcome == "written":
                report.works_written.append(work_id)
            elif outcome == "read_only":
                report.works_read_only.append(work_id)
        return report
