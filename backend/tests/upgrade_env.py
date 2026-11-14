"""An exported chapter and a better copy of it, for the same-path upgrade tests (EF-EXP-016).

Built on `export_env`: real files, a real migrated database, the real pipeline. The better copy is
a second Variant of the same Unit (other pages), received like any archive; the upgrade is asked
for its `content`.
"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from shelfhold.export.application.upgrade import SamePathUpgrade, UpgradeRequest
from shelfhold.export.domain.physical_roots import RECEIVED_DIR, RECYCLE_DIR
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, identity
from shelfhold.library.public import PassthroughStore
from tests.export_env import ExportEnv, Series
from tests.integration.test_schema_catalog import add_variant
from tests.library_env import cbz_bytes
from tests.locks_env import work_lock
from tests.null_tombstones import NONE_PURGED


def digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass
class Exported:
    series: Series
    unit_id: str
    export_item_id: str
    target: Path
    old_file_hash: str
    old_content_hash: str


def export_one(env: ExportEnv) -> Exported:
    """One chapter, exported by the pipeline: one file under the destination."""
    series = env.new_series()
    unit_id = env.add_chapter(series, "1")
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    (target,) = env.files()
    record = export.export_record
    with env.engine.connect() as connection:
        row = connection.execute(
            sa.select(record).where(record.c.shelf_destination_id == env.destination_id)
        ).one()
    return Exported(
        series, unit_id, row.export_item_id, target, str(row.file_hash), str(row.content_hash)
    )


def better_copy(env: ExportEnv, exported: Exported, *, tag: bytes = b"better") -> str:
    """Another Variant of the same Unit with other pages, received: its `content.id`."""
    with write_transaction(env.engine) as connection:
        variant = add_variant(
            connection, exported.unit_id, exported.series.binding_id, f"ch-{new_ulid()}"
        )
    pages = (("1-001.png", b"\x89PNG\r\n\x1a\n" + tag + b"\x00" * 32),)
    PassthroughStore(
        env.engine,
        env.lib.fsops,
        env.lib.locate,
        env.lib.data,
        tombstones=NONE_PURGED,
        hold_work=work_lock(env.engine),
    ).ingest(
        unit_id=exported.unit_id,
        variant_id=variant,
        staged=env.lib.stage(cbz_bytes(pages)),
        dest=str(env.lib.manga / RECEIVED_DIR / f"{exported.unit_id}-{new_ulid()}.cbz"),
    )
    with env.engine.connect() as connection:
        return str(
            connection.execute(
                sa.text("SELECT id FROM content WHERE variant_id = :v"), {"v": variant}
            ).scalar_one()
        )


def service(env: ExportEnv, *, scans: list[str] | None = None) -> SamePathUpgrade:
    return SamePathUpgrade(
        env.engine,
        env.lib.fsops,
        locate=env.lib.locate,
        data_dir=env.lib.data,
        clock=env.clock,
        request_scan=scans.append if scans is not None else None,
    )


def request(
    env: ExportEnv,
    exported: Exported,
    content_id: str,
    *,
    reason: str = "better_version",
    automatic: bool = True,
    typed: str | None = None,
) -> UpgradeRequest:
    return UpgradeRequest(
        destination_id=env.destination_id,
        export_item_id=exported.export_item_id,
        content_id=content_id,
        reason=reason,
        automatic=automatic,
        typed=typed,
    )


def mark_read(env: ExportEnv, exported: Exported) -> None:
    """A reader finished the exported file: `reading_progress` says `read` for the Unit."""
    user_id = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(identity.user).values(id=user_id, username=f"reader-{user_id}", role="member")
        )
        connection.execute(
            sa.text(
                "INSERT INTO reading_position (id, user_id, carrier_type, carrier_id, "
                "content_hash, locator, percent, client_ts, server_ts) VALUES "
                "(:id, :user, 'export_item', :item, :hash, '{}', 1.0, :ts, :ts)"
            ),
            {
                "id": new_ulid(), "user": user_id, "item": exported.export_item_id,
                "hash": exported.old_content_hash, "ts": env.clock.now(),
            },
        )  # fmt: skip


def make_irreplaceable(env: ExportEnv) -> None:
    """No active binding lists the Unit any more: nothing upstream can give it back."""
    with write_transaction(env.engine) as connection:
        connection.execute(sa.update(catalog.unit_variant).values(removed_at=env.clock.now()))


def bin_files(env: ExportEnv) -> list[Path]:
    bin_dir = env.lib.library / RECYCLE_DIR
    return sorted(p for p in bin_dir.rglob("*") if p.is_file()) if bin_dir.exists() else []


def record_of(env: ExportEnv, exported: Exported) -> sa.Row[tuple[object, ...]]:
    record = export.export_record
    with env.engine.connect() as connection:
        return connection.execute(
            sa.select(record).where(
                record.c.shelf_destination_id == env.destination_id,
                record.c.export_item_id == exported.export_item_id,
            )
        ).one()


__all__ = [
    "Exported",
    "better_copy",
    "bin_files",
    "digest",
    "export_one",
    "make_irreplaceable",
    "mark_read",
    "record_of",
    "request",
    "service",
]
