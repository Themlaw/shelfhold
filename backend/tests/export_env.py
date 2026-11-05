"""A shelf, a Kavita-like tree destination and an export pipeline over the synthetic library.

Real files and a real migrated database: the received archives are ingested by the passthrough
store into an inbox folder next to the destination root, the pipeline renders them (ComicInfo
last) into the shelf's tree. Nothing is mocked but the clock and the `plugin/parse` replica.
"""

import hashlib
import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa

from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.domain.naming import DEFAULT_TEMPLATE
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, platform
from shelfhold.library.public import PassthroughStore
from tests.fakes.kavita.plugin_parse import simulate_parse
from tests.fakes_clock import ManualClock
from tests.integration.test_schema_catalog import (
    add_binding,
    add_edition,
    add_source_item,
    add_unit,
    add_variant,
    add_work,
)
from tests.intents_env import NOW
from tests.library_env import LibEnv, cbz_bytes, lib_env

ROOT_SUBPATH = "Kavita"


def sha(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass
class Series:
    """A Work with one Edition (and its binding) on the shelf."""

    work_id: str
    edition_id: str
    binding_id: str


@dataclass
class ExportEnv:
    lib: LibEnv
    clock: ManualClock
    target_id: str
    shelf_id: str
    destination_id: str
    remote_library_id: str
    pipeline: ExportPipeline
    scans: list[str] = field(default_factory=list)
    count: int = 0

    @property
    def root(self) -> Path:
        return self.lib.manga / ROOT_SUBPATH

    @property
    def engine(self) -> sa.Engine:
        return self.lib.engine

    def files(self) -> list[Path]:
        return self.lib.files_under(self.root)

    def rows(self, table: sa.Table) -> list[sa.Row[tuple[object, ...]]]:
        with self.engine.connect() as connection:
            return list(connection.execute(sa.select(table)))

    def new_series(
        self, title: str = "Dr Stone", *, lang: str = "fr", shelf_id: str | None = None
    ) -> Series:
        """Another Work, with its Edition on the shelf (`shelf_id=""` leaves it unshelved)."""
        with self.engine.begin() as connection:
            work = add_work(connection, title=title, sort_title=title.lower())
            edition = add_edition(connection, work, lang=lang, shelf_id=shelf_id or self.shelf_id)
            item = add_source_item(connection, remote_id=f"series-{new_ulid()}")
            binding = add_binding(connection, work, edition, item)
        return Series(work, edition, binding)

    def main_series(self) -> Series:
        chain = self.lib.chain
        with self.engine.begin() as connection:
            connection.execute(
                sa.update(catalog.edition)
                .where(catalog.edition.c.id == chain["edition"])
                .values(shelf_id=self.shelf_id)
            )
        return Series(chain["work"], chain["edition"], chain["binding"])

    def add_chapter(
        self, series: Series, number: str, *, watermarked: bool = False, tag: bytes = b""
    ) -> str:
        """A Unit with its Variant and its received archive (pages differ per chapter)."""
        self.count += 1
        with self.engine.begin() as connection:
            unit = add_unit(connection, series.edition_id, number)
            variant = add_variant(connection, unit, series.binding_id, f"ch-{new_ulid()}")
        pages = (
            (f"{number}-001.png", b"\x89PNG\r\n\x1a\n" + tag + number.encode() + b"\x00" * 16),
        )
        received = self.lib.manga / ".received" / f"{unit}.cbz"
        PassthroughStore(self.engine, self.lib.fsops, self.lib.locate, self.lib.data).ingest(
            unit_id=unit,
            variant_id=variant,
            staged=self.lib.stage(cbz_bytes(pages)),
            dest=str(received),
        )
        if watermarked:
            with self.engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE content SET watermarked = 1 WHERE unit_id = :u"), {"u": unit}
                )
        return unit


@contextmanager
def export_env(*, max_age_rating: int | None = 18, transport: str = "tree") -> Iterator[ExportEnv]:
    with lib_env() as lib:
        target_id, shelf_id = new_ulid(), new_ulid()
        destination_id, library_id = new_ulid(), new_ulid()
        path_map = [{"local": str(lib.manga), "remote": "/manga", "mode": "rw"}]
        with lib.engine.begin() as connection:
            connection.execute(
                sa.insert(platform.instance).values(instance_id=new_ulid(), jurisdiction="FR")
            )
            connection.execute(
                sa.insert(export.export_target).values(
                    id=target_id, kind="kavita", name="Kavita", path_map=path_map
                )
            )
            connection.execute(
                sa.insert(export.shelf).values(
                    id=shelf_id, name="Adultes", phrase="Mes mangas", max_age_rating=max_age_rating
                )
            )
            connection.execute(
                sa.insert(export.shelf_destination).values(
                    id=destination_id,
                    shelf_id=shelf_id,
                    target_id=target_id,
                    transport=transport,
                    egress="foyer",
                    root_subpath=ROOT_SUBPATH,
                    template=DEFAULT_TEMPLATE.dumps(),
                    template_origin=f"default@{DEFAULT_TEMPLATE.version}",
                    kavita_library_type="manga",
                )
            )
            connection.execute(
                sa.insert(export.remote_library).values(
                    id=library_id, export_target_id=target_id, remote_ref="1"
                )
            )
            connection.execute(
                sa.insert(export.shelf_destination_remote_library).values(
                    shelf_destination_id=destination_id,
                    remote_library_id=library_id,
                    export_target_id=target_id,
                )
            )
        (lib.manga / ROOT_SUBPATH).mkdir()
        (lib.data / "tmp").mkdir(exist_ok=True)  # the staging folder `ensure_volumes` creates
        clock = ManualClock(NOW)
        scans: list[str] = []
        pipeline = ExportPipeline(
            lib.engine,
            lib.fsops,
            locate=lib.locate,
            data_dir=lib.data,
            clock=clock,
            parse=simulate_parse,
            request_scan=scans.append,
        )
        yield ExportEnv(
            lib, clock, target_id, shelf_id, destination_id, library_id, pipeline, scans
        )


def add_second_shelf(env: ExportEnv, *, max_age_rating: int | None = 99) -> tuple[str, str]:
    """Another shelf with its own destination folder, linked to the same remote library."""
    shelf_id, destination_id = new_ulid(), new_ulid()
    (env.lib.manga / "Kavita2").mkdir(exist_ok=True)
    with env.engine.begin() as connection:
        connection.execute(
            sa.insert(export.shelf).values(
                id=shelf_id, name="Adultes 2", phrase="Autres", max_age_rating=max_age_rating
            )
        )
        connection.execute(
            sa.insert(export.shelf_destination).values(
                id=destination_id,
                shelf_id=shelf_id,
                target_id=env.target_id,
                transport="tree",
                egress="foyer",
                root_subpath="Kavita2",
                template=DEFAULT_TEMPLATE.dumps(),
                template_origin=f"default@{DEFAULT_TEMPLATE.version}",
                kavita_library_type="manga",
            )
        )
        connection.execute(
            sa.insert(export.shelf_destination_remote_library).values(
                shelf_destination_id=destination_id,
                remote_library_id=env.remote_library_id,
                export_target_id=env.target_id,
            )
        )
    return shelf_id, destination_id


def state_of(env: ExportEnv) -> dict[str, object]:
    """What a no-op run must leave exactly as it was: files, manifest, journal, series."""
    files = {
        str(p.relative_to(env.root)): (p.stat().st_mtime_ns, p.stat().st_ino) for p in env.files()
    }
    with env.engine.connect() as connection:
        manifest = (
            connection.execute(sa.text("SELECT * FROM export_record ORDER BY export_item_id"))
            .mappings()
            .all()
        )
        intents = connection.execute(sa.text("SELECT count(*) FROM fs_intent")).scalar_one()
        series = connection.execute(sa.text("SELECT * FROM export_series")).mappings().all()
        links = connection.execute(sa.text("SELECT * FROM edition_export_series")).mappings().all()
    return {
        "files": files,
        "manifest": json.dumps([dict(r) for r in manifest], default=str),
        "intents": intents,
        "series": json.dumps([dict(r) for r in series], default=str),
        "links": json.dumps([dict(r) for r in links], default=str),
    }


__all__ = [
    "ExportEnv",
    "Series",
    "add_second_shelf",
    "export_env",
    "sha",
    "state_of",
]
