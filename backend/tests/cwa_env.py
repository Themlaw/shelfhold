"""A shelf bound to a CWA drop box, over the synthetic export environment (M4 L09, EF-EXP-022).

The ingestion folder is a folder of the library; the EPUBs are made here (no network, no third
party); the host serves their bytes from the verified derivative, as it does for any destination.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from shelfhold_sdk.testing.corpus.trapped_epubs.build import epub_bytes, xhtml

from shelfhold.export.application.fan_out import EpubFile, FanOut, HostContentStore
from shelfhold.export.targets.cwa.source import CwaSource
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.byte_source_env import add_format, unit_with_item
from tests.export_env import ExportEnv, Series
from tests.fan_out_env import add_target, service
from tests.plan_executor_env import Probe

INGEST = "ingest"


@dataclass
class Book:
    unit_id: str
    item_id: str
    data: bytes
    epub: EpubFile


def add_book(env: ExportEnv, series: Series, number: str, store: dict[str, EpubFile]) -> Book:
    """A Unit with an EPUB derivative (and its archive): an export item the drop box may take."""
    unit = unit_with_item(env, series, number)
    data = epub_bytes(xhtml(f"<p>book {number} of {series.work_id}</p>"))
    content_id = add_format(env, unit, "epub", data)
    path = env.lib.manga / "received" / f"{unit.unit_id}.epub"
    epub = EpubFile(str(path), "sha256:" + hashlib.sha256(data).hexdigest(), content_id)
    store[unit.unit_id] = epub
    return Book(unit.unit_id, unit.item_id, data, epub)


def cwa_target(env: ExportEnv, *, partitioning: str = "none") -> str:
    """A drop box whose folder is a folder of the library (`kind = cwa`); what it says about its
    partitioning counts for nothing: a drop box keeps no account apart."""
    return add_target(env, kind="cwa", partitioning=partitioning)


def bind_cwa(
    env: ExportEnv,
    *,
    subpath: str = INGEST,
    shelf_id: str | None = None,
    target_id: str | None = None,
) -> str:
    (env.lib.manga / subpath).mkdir(parents=True, exist_ok=True)
    return service(env).bind_destination(
        shelf_id=shelf_id or env.shelf_id,
        target_id=target_id or cwa_target(env),
        transport="dropbox",
        plugin_egress="foyer",
        root_subpath=subpath,
    )


def rig(env: ExportEnv, store: dict[str, EpubFile]) -> tuple[FanOut, CwaSource]:
    """The fan-out of the shelf with the drop box as its source of plans."""
    source = CwaSource(env.engine)
    host = HostContentStore(
        env.engine,
        capabilities=source.capabilities,
        epub_path=lambda unit_id: store.get(unit_id),
    )
    source.attach(host)
    env.pipeline.use_plan_executor(contents=host, probe=Probe(None))
    return FanOut(env.engine, env.pipeline, source, clock=env.clock), source


def folder(env: ExportEnv, subpath: str = INGEST) -> Path:
    return env.lib.manga / subpath


def names(env: ExportEnv, subpath: str = INGEST) -> list[str]:
    return sorted(p.name for p in folder(env, subpath).iterdir())


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


def other_shelf_item(env: ExportEnv, store: dict[str, EpubFile]) -> Book:
    """An EPUB filed on **another** shelf of the instance (the one nobody bound to the box)."""
    shelf_id = new_ulid()
    with write_transaction(env.engine) as connection:
        connection.execute(
            sa.insert(export.shelf).values(
                id=shelf_id, name="Autre", phrase="Autre étagère", max_age_rating=99
            )
        )
    series = env.new_series("Other Series", shelf_id=shelf_id)
    return add_book(env, series, "1", store)
