"""L50: a real instance (database, keyring, roots, lease) with a shelf linked to the simulated
Kavita and chapters received into its inbox. Only Kavita is simulated; the jobs, the pipeline and
the file system are the production ones."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

import shelfhold.runtime_jobs as rj
from shelfhold.config import Settings
from shelfhold.export.application.pipeline import ApplyReport
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.linking import ShelfLinker
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import PassthroughStore
from shelfhold.ops.events import BusTargetEvents
from tests.fakes.kavita.plugin_parse import simulate_parse
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)
from tests.l41_env import Instance, instance
from tests.library_env import cbz_bytes

REMOTE_ROOT = "/manga/Kavita"


@dataclass
class Series:
    work_id: str
    edition_id: str
    binding_id: str


@dataclass
class Seeded:
    inst: Instance
    destination_id: str
    shelf_id: str
    tmp: Path
    scans: rj.ScanRelay = field(default_factory=rj.ScanRelay)

    def new_series(self, title: str = "Dr Stone") -> Series:
        with self.inst.engine.begin() as connection:
            work = add_work(connection, title=title, sort_title=title.lower())
            edition = add_edition(connection, work, lang="fr", shelf_id=self.shelf_id)
            item = add_source_item(connection, remote_id=f"series-{new_ulid()}")
            binding = add_binding(connection, work, edition, item)
        return Series(work, edition, binding)

    def add_chapter(self, series: Series, number: str) -> str:
        """A Unit received through the production store, into the inbox of the first root."""
        with self.inst.engine.begin() as connection:
            unit = add_unit(connection, series.edition_id, number)
            variant = add_variant(connection, unit, series.binding_id, f"ch-{new_ulid()}")
        page = b"\x89PNG\r\n\x1a\n" + number.encode() + b"\x00" * 16
        staged = self.tmp / "staging" / f"{unit}.cbz"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(cbz_bytes(((f"{number}-001.png", page),)))
        with self.inst.comp.write_context() as w:
            PassthroughStore(
                self.inst.engine, w.fsops, w.loaded.locate, self.inst.settings.data_dir,
                tombstones=w.tombstones,
            ).ingest(
                unit_id=unit, variant_id=variant, staged=staged, dest=w.received_path(variant)
            )  # fmt: skip
        return unit

    def export(self) -> ApplyReport:
        """What the export job does for this destination once the names are checked."""
        pipeline, _reconciler, _fsops = rj.build_pipeline(self.inst.comp, self.scans)
        pipeline.bind_parse(simulate_parse, verified=True)
        return pipeline.run(self.destination_id, job_id=new_ulid())

    def rows(self, table: sa.Table) -> list[sa.Row[tuple[object, ...]]]:
        with self.inst.engine.connect() as connection:
            return list(connection.execute(sa.select(table)))

    def exported_files(self) -> list[Path]:
        return sorted(p for p in (self.inst.library / "Kavita").rglob("*.cbz") if p.is_file())


@contextmanager
def seeded(settings: Settings, tmp: Path) -> Iterator[Seeded]:
    with instance(settings, tmp) as inst:
        inst.configure()
        destination = rj.tree_destinations(inst.comp)[0]
        with inst.engine.connect() as connection:
            shelf_id = connection.execute(
                sa.select(export.shelf_destination.c.shelf_id).where(
                    export.shelf_destination.c.id == destination.id
                )
            ).scalar_one()
        auditor = AccessAuditor(
            inst.engine, inst.comp.clock, BusTargetEvents(inst.engine, inst.comp.bus)
        )
        linker = ShelfLinker(inst.engine, inst.comp.clock, auditor)
        asyncio.run(
            linker.link(
                inst.comp.kavita_client(destination.target_id),
                destination.id,
                library_name="Adultes",
            )
        )
        yield Seeded(inst, destination.id, shelf_id, tmp)
