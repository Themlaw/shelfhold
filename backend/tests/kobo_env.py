"""A shelf bound to a Kobo folder, over the synthetic export environment (M6 L16, EF-EXP-021).

The folder is the one the household's sync tool, or the mounted reader, reads; the books are made
here (no network, no third party). The host serves each one from its verified derivative, as it
does for any destination, and the KEPUB is made from those bytes by the rendition cache of M5
L20b — never from the received original.
"""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa

from shelfhold.export.application.fan_out import EpubFile, FanOut, HostContentStore
from shelfhold.export.targets.kobo.source import KoboSource
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export
from shelfhold.library.application.renditions import RenditionService
from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, xhtml
from tests.byte_source_env import add_format, unit_with_item
from tests.export_env import ExportEnv, Series
from tests.fan_out_env import add_target, service
from tests.null_tombstones import NONE_PURGED
from tests.plan_executor_env import Probe

KOBO = "Kobo"
CHAPTER = "OEBPS/ch1.xhtml"  # the spine document the corpus builds around a paragraph


def member(data: bytes, name: str) -> bytes:
    """One entry of an EPUB, decompressed: what a reader opens, not the compressed bytes."""
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        return archive.read(name)


@dataclass
class Book:
    unit_id: str
    item_id: str
    original: bytes  # the received original, as the library holds it: never served
    data: bytes  # the neutralized derivative the host serves
    epub: EpubFile


def add_book(env: ExportEnv, series: Series, number: str, store: dict[str, EpubFile]) -> Book:
    """A Unit with an EPUB content (the received original) and its verified derivative: an
    export item a Kobo folder may take, served from the derivative alone."""
    unit = unit_with_item(env, series, number)
    original = epub_bytes(xhtml(f"<p>original {number} of {series.work_id}</p>"))
    derivative = epub_bytes(xhtml(f"<p>book {number} of {series.work_id}</p>"))
    content_id = add_format(env, unit, "epub", original)
    path = env.lib.manga / "received" / f"{unit.unit_id}.derivative.epub"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(derivative)
    epub = EpubFile(str(path), "sha256:" + hashlib.sha256(derivative).hexdigest(), content_id)
    store[unit.unit_id] = epub
    return Book(unit.unit_id, unit.item_id, original, derivative, epub)


def kobo_target(env: ExportEnv, *, partitioning: str = "none") -> str:
    """A folder a Kobo reads (`kind = kobo`); what it says about its partitioning counts for
    nothing: a folder keeps no account apart."""
    return add_target(env, kind="kobo", partitioning=partitioning)


def bind_kobo(
    env: ExportEnv,
    *,
    subpath: str = KOBO,
    shelf_id: str | None = None,
    target_id: str | None = None,
) -> str:
    (env.lib.manga / subpath).mkdir(parents=True, exist_ok=True)
    return service(env).bind_destination(
        shelf_id=shelf_id or env.shelf_id,
        target_id=target_id or kobo_target(env),
        transport="tree",
        plugin_egress="foyer",
        root_subpath=subpath,
    )


def renditions(env: ExportEnv) -> RenditionService:
    """The rendition cache of M5 L20b over the library of the environment."""
    return RenditionService(env.engine, env.lib.data, env.clock, tombstones=NONE_PURGED)


def rig(
    env: ExportEnv, store: dict[str, EpubFile], renditions: RenditionService
) -> tuple[FanOut, KoboSource]:
    """The fan-out of the shelf with the Kobo folder as its source of plans."""
    source = KoboSource(env.engine)
    host = HostContentStore(
        env.engine,
        capabilities=source.capabilities,
        epub_path=lambda unit_id: store.get(unit_id),
        kepub=source.kepub,
        kepub_maker=renditions.kepub_bytes,
    )
    source.attach(host)
    env.pipeline.use_plan_executor(contents=host, probe=Probe(None))
    return FanOut(env.engine, env.pipeline, source, clock=env.clock), source


def folder(env: ExportEnv, subpath: str = KOBO) -> Path:
    return env.lib.manga / subpath


def tree_files(env: ExportEnv, subpath: str = KOBO) -> list[Path]:
    """The files of the Kobo folder, as the reader mounts it."""
    return sorted(p for p in folder(env, subpath).rglob("*") if p.is_file())


def records(env: ExportEnv, destination_id: str) -> list[sa.Row[tuple[object, ...]]]:
    with env.engine.connect() as connection:
        return list(
            connection.execute(
                sa.select(export.export_record)
                .where(export.export_record.c.shelf_destination_id == destination_id)
                .order_by(export.export_record.c.export_item_id)
            )
        )


def lower_ceiling(env: ExportEnv, ceiling: int) -> None:
    """The admin lowers the ceiling of the shelf after the binding was made."""
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.update(export.shelf)
            .where(export.shelf.c.id == env.shelf_id)
            .values(max_age_rating=ceiling)
        )
