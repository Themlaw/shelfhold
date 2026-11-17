"""Chapters exported one by one, then assembled into a volume (M4 L10, EF-EXP-019, EF-BIB-044).

Built on `export_env`: real files, a real migrated database, the real pipeline. A chapter has as
many pages as the test says (a webtoon chapter is long), and what the pipeline exports of it is
one file under the destination, which the assembly then replaces by one volume.
"""

import hashlib
import io
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

from shelfhold.export.application.assembly import AssemblyRequest, VolumeAssembly
from shelfhold.export.domain.physical_roots import RECEIVED_DIR, RECYCLE_DIR
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export, identity
from shelfhold.library.public import PassthroughStore
from tests.export_env import ExportEnv, Series
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.library_env import cbz_bytes
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED

PNG = b"\x89PNG\r\n\x1a\n"


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def chapter(
    env: ExportEnv,
    series: Series,
    number: str,
    pages: int,
    *,
    first: tuple[str, bytes] | None = None,
) -> str:
    """A Unit with its Variant and its received archive of `pages` pages (each one its own)."""
    with write_transaction(env.engine) as connection:
        unit = add_unit(connection, series.edition_id, number)
        variant = add_variant(connection, unit, series.binding_id, f"ch-{new_ulid()}")
    body = tuple(
        (f"{number}-{page:03d}.png", PNG + f"{number}/{page}".encode() + b"\x00" * 16)
        for page in range(1, pages + 1)
    )
    if first is not None:  # the first page is another format (an AVIF)
        body = (first, *body[1:])
    PassthroughStore(
        env.engine,
        env.lib.fsops,
        env.lib.locate,
        env.lib.data,
        tombstones=NONE_PURGED,
        hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=unit,
        variant_id=variant,
        staged=env.lib.stage(cbz_bytes(body)),
        dest=str(env.lib.manga / RECEIVED_DIR / f"{unit}.cbz"),
    )
    return unit


@dataclass
class Chapters:
    series: Series
    units: list[str]
    pages: list[int]
    items: list[str]  # the export item of each chapter, in reading order
    targets: list[Path]  # the file served for each
    old_bytes: list[bytes]
    old_hashes: list[str] = field(default_factory=list)
    content_hashes: list[str] = field(default_factory=list)  # what a position is anchored on


def exported_chapters(
    env: ExportEnv,
    pages: list[int],
    *,
    series: Series | None = None,
    first: int = 1,
    first_page: dict[int, tuple[str, bytes]] | None = None,  # chapter index -> its first page
) -> Chapters:
    """`len(pages)` chapters of one Edition, exported by the pipeline."""
    series = series or env.new_series()
    units = [
        chapter(env, series, str(first + n), count, first=(first_page or {}).get(n))
        for n, count in enumerate(pages)
    ]
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    record, cover = export.export_record, export.unit_coverage
    items: list[str] = []
    targets: list[Path] = []
    hashes: list[str] = []
    contents: list[str] = []
    with env.engine.connect() as connection:
        for unit in units:
            row = connection.execute(
                sa.select(
                    record.c.export_item_id,
                    record.c.rel_path,
                    record.c.file_hash,
                    record.c.content_hash,
                )
                .join(
                    cover,
                    sa.and_(
                        cover.c.carrier_type == "export_item",
                        cover.c.carrier_id == record.c.export_item_id,
                    ),
                )
                .where(cover.c.unit_id == unit, record.c.shelf_destination_id == env.destination_id)
            ).one()
            items.append(str(row.export_item_id))
            targets.append(env.root / str(row.rel_path))
            hashes.append(str(row.file_hash))
            contents.append(str(row.content_hash))
    return Chapters(
        series, units, pages, items, targets, [t.read_bytes() for t in targets], hashes, contents
    )


def service(env: ExportEnv, *, scans: list[str] | None = None) -> VolumeAssembly:
    return VolumeAssembly(
        env.engine,
        env.lib.fsops,
        locate=env.lib.locate,
        data_dir=env.lib.data,
        clock=env.clock,
        request_scan=scans.append if scans is not None else None,
    )


def request(
    env: ExportEnv, chapters: Chapters, *, volume: str = "1", items: list[str] | None = None
) -> AssemblyRequest:
    return AssemblyRequest(
        destination_id=env.destination_id,
        export_item_ids=tuple(items if items is not None else chapters.items),
        volume=volume,
    )


def bin_files(env: ExportEnv) -> list[Path]:
    bin_dir = env.lib.library / RECYCLE_DIR
    return sorted(p for p in bin_dir.rglob("*") if p.is_file()) if bin_dir.exists() else []


def pages_of(path: Path) -> list[str]:
    """The image entries of a CBZ, in the order they are stored, and `ComicInfo.xml` last."""
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        return archive.namelist()


def page_bytes(path: Path) -> list[bytes]:
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        return [archive.read(n) for n in archive.namelist() if n != "ComicInfo.xml"]


def reader(env: ExportEnv, name: str = "reader", role: str = "member", **extra: object) -> str:
    user_id = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(identity.user).values(
                id=user_id, username=f"{name}-{user_id}", role=role, **extra
            )
        )
    return user_id


def position(
    env: ExportEnv, user: str, item: str, content_hash: str, units: list[str], page: int,
    *, source: str = "device", percent: float | None = None,
) -> None:  # fmt: skip
    """A reading position of `user` on the file of `item`, at `page`."""
    import json

    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.text(
                "INSERT INTO reading_position (id, user_id, carrier_type, carrier_id, "
                "content_hash, locator, percent, client_ts, server_ts, source, covered_units) "
                "VALUES (:id, :user, 'export_item', :item, :hash, :locator, :percent, :ts, :ts, "
                ":source, :units)"
            ),
            {
                "id": new_ulid(), "user": user, "item": item, "hash": content_hash,
                "locator": json.dumps({"page": page}), "ts": env.clock.now(), "source": source,
                "units": json.dumps(units), "percent": percent,
            },
        )  # fmt: skip


def progress_of(env: ExportEnv, user: str) -> dict[str, str]:
    with env.engine.connect() as connection:
        return {
            str(r.unit_id): str(r.state)
            for r in connection.execute(
                sa.text("SELECT unit_id, state FROM reading_progress WHERE user_id = :u"),
                {"u": user},
            )
        }


__all__ = [
    "Chapters",
    "bin_files",
    "chapter",
    "digest",
    "exported_chapters",
    "page_bytes",
    "pages_of",
    "position",
    "progress_of",
    "reader",
    "request",
    "service",
]
