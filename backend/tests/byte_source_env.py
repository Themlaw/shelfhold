"""Bytes of a fan-out (M4 L06b, EF-BIB-045, EF-BIB-048, EF-EXP-046): the places a copy of a Unit can
be read from, set up on the synthetic library with real files and rows.

`library_copy_of` is what the received archive left; the helpers below take it away, put a copy in
`/store` or in a destination folder, and add another format to the Variant of a Unit.
"""

import os
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from shelfhold.export.application.export_items import create_export_item
from shelfhold.export.application.fan_out import AppliedFile, record_applied
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import replica as replica_schema
from shelfhold.infrastructure.schema import storage
from shelfhold.library.public import (
    ExportSource,
    drop_replicas,
    export_sources,
    record_destination_copy,
    record_replica,
)
from shelfhold.sdk.export_target import TargetCapabilities
from tests.export_env import ExportEnv, Series, sha
from tests.fan_out_env import add_target

CBZ = "application/vnd.comicbook+zip"


@dataclass
class Unit:
    unit_id: str
    item_id: str
    source: ExportSource
    data: bytes  # the received archive, as the library holds it


def unit_with_item(env: ExportEnv, series: Series, number: str = "1") -> Unit:
    unit = env.add_chapter(series, number)
    with write_transaction(env.engine) as connection:
        item = create_export_item(
            connection, edition_id=series.edition_id, unit_id=unit, kind="unit", frozen_attrs={}
        )
        found = export_sources(connection, [unit]).get(unit)
    assert found is not None and found.local_path is not None
    return Unit(unit, item, found, Path(found.local_path).read_bytes())


def drop_library_copy(env: ExportEnv, unit: Unit) -> None:
    """The library copy is gone: the file and its row."""
    with write_transaction(env.engine) as connection:
        ids = list(
            connection.execute(
                sa.select(replica_schema.replica.c.id).where(
                    replica_schema.replica.c.unit_id == unit.unit_id,
                    replica_schema.replica.c.location == "library",
                )
            ).scalars()
        )
        drop_replicas(connection, ids)
    Path(str(unit.source.local_path)).unlink()


def put_store_copy(env: ExportEnv, unit: Unit, data: bytes | None = None) -> Path:
    """An independent copy in `/store`, verified (its own inode, the hash that was stored)."""
    data = unit.data if data is None else data
    rel = f"{unit.unit_id}.cbz"
    path = env.lib.store / rel
    path.write_bytes(data)
    stat = path.stat()
    with write_transaction(env.engine) as connection:
        record_replica(
            connection, unit_id=unit.unit_id, location="store",
            content_hash=unit.source.content_hash, file_hash=sha(data), independent=True,
            physical_root_id=env.lib.store_root, rel_path=rel, dev=stat.st_dev, ino=stat.st_ino,
            size=stat.st_size, mtime_ns=stat.st_mtime_ns, verified_at=env.clock.now(),
        )  # fmt: skip
    return path


def put_destination_copy(
    env: ExportEnv, destination_id: str, unit: Unit, data: bytes, *, rel: str | None = None,
    folder: str = "Kavita",
) -> Path:  # fmt: skip
    """A file a binding wrote, as its manifest and the registry know it. The bytes are the ones
    given (the canonical archive, or a rendering of it)."""
    rel = rel or f"Series/{unit.unit_id}.cbz"
    path = env.lib.manga / folder / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stat = path.stat()
    with write_transaction(env.engine) as connection:
        record_applied(
            connection, destination_id=destination_id,
            files=[AppliedFile(unit.item_id, rel, sha(data), len(data), stat.st_mtime_ns)],
            now=env.clock.now(),
        )  # fmt: skip
        root_id, rel_in_root = env.lib.locate(str(path))
        record_destination_copy(
            connection, unit_id=unit.unit_id, export_item_id=unit.item_id,
            content_hash=unit.source.content_hash or "", file_hash=sha(data),
            physical_root_id=root_id, rel_path=rel_in_root, size=stat.st_size,
            mtime_ns=stat.st_mtime_ns, ino=stat.st_ino, dev=stat.st_dev,
            verified_at=env.clock.now(),
        )  # fmt: skip
    return path


def add_format(env: ExportEnv, unit: Unit, fmt: str, data: bytes) -> str:
    """A second `content` of the Variant of the Unit (a PDF next to its CBZ), filed in the library
    with its copy. Returns the id of the content."""
    media = {"pdf": "application/pdf", "epub": "application/epub+zip"}.get(fmt, f"application/{fmt}")
    content_id, content_hash = new_ulid(), sha(b"payload-" + data)
    rel = f"{unit.unit_id}.{fmt}"
    path = env.lib.manga / "received" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    stat = path.stat()
    with write_transaction(env.engine) as connection:
        variant = connection.execute(
            sa.select(storage.content.c.variant_id).where(storage.content.c.unit_id == unit.unit_id)
        ).scalar_one()
        connection.execute(
            sa.insert(storage.content).values(
                id=content_id, unit_id=unit.unit_id, variant_id=variant, kind="file",
                media_type=media, format=fmt, retention="passthrough",
                original_sha256=sha(data), content_hash=content_hash, content_hash_by="t1",
                store_provenance="received", size=len(data),
            )
        )  # fmt: skip
        root_id, rel_in_root = env.lib.locate(str(path))
        record_replica(
            connection, unit_id=unit.unit_id, location="library", content_hash=content_hash,
            file_hash=sha(data), independent=True, physical_root_id=root_id, rel_path=rel_in_root,
            dev=stat.st_dev, ino=stat.st_ino, size=stat.st_size, mtime_ns=stat.st_mtime_ns,
            verified_at=env.clock.now(),
        )  # fmt: skip
    return content_id


def other_target(env: ExportEnv, caps: TargetCapabilities, registry: dict[str, TargetCapabilities]) -> str:
    """Another server of the library, whose capabilities the test chooses."""
    target_id = add_target(env)
    registry[target_id] = caps
    return target_id


def inode(path: Path) -> tuple[int, int, int]:
    stat = os.stat(path)
    return stat.st_dev, stat.st_ino, stat.st_nlink
