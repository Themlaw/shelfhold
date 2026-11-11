"""L09 (M2): EPUB Works exported to a Kavita Books library, over the synthetic export environment.

A book is an EPUB ingested by the derivative pipeline of M2 L03 (an `untrusted` original kept in
/store, its verified derivative filed in the library); the pipeline exports what `servable_epub`
names. Kavita is the simulated server: no network, every byte made here.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from shelfhold.export.application.locks import work_lock_for
from shelfhold.export.domain.physical_roots import RECEIVED_DIR
from shelfhold.export.targets.kavita.arrival import ArrivalPoller
from shelfhold.export.targets.kavita.series import SeriesAgeGate
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from shelfhold.library.public import UntrustedEpubStore
from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, xhtml
from tests.export_env import ExportEnv, Series
from tests.integration.test_schema_catalog import add_unit, add_variant
from tests.kavita_env import run
from tests.kavita_export_env import REMOTE_ROOT, Wired
from tests.null_tombstones import NONE_PURGED

# Kavita's `AgeRating`
UNKNOWN, EVERYONE_10, TEEN, MATURE_17 = 0, 5, 8, 10


def books_library(env: ExportEnv, library_type: str = "book") -> None:
    """The admin declared the Kavita library of the destination as a Books library (D19)."""
    with env.engine.begin() as connection:
        connection.execute(
            sa.update(export.shelf_destination)
            .where(export.shelf_destination.c.id == env.destination_id)
            .values(kavita_library_type=library_type)
        )


def add_book(env: ExportEnv, series: Series, number: str) -> str:
    """A Unit whose content is an EPUB, filed through the derivative pipeline (L03)."""
    with env.engine.begin() as connection:
        unit = add_unit(connection, series.edition_id, number)
        variant = add_variant(connection, unit, series.binding_id, f"bk-{new_ulid()}")
    store = UntrustedEpubStore(
        env.engine, env.lib.fsops, env.lib.locate, env.lib.data,
        store_dir=str(env.lib.store), tombstones=NONE_PURGED,
        hold_work=work_lock_for(env.engine, clock=SystemClock(), timeout=0.0),
    )  # fmt: skip
    data = epub_bytes(xhtml(f"<p>book {number} of {series.work_id}</p>"))
    store.ingest(
        unit_id=unit, variant_id=variant, staged=env.lib.stage(data, f"{unit}.epub"),
        dest=str(env.lib.manga / RECEIVED_DIR / f"{unit}.epub"),
    )  # fmt: skip
    return unit


def written(env: ExportEnv) -> list[str]:
    """The relative paths of the files the destination holds (manifest lines written)."""
    return sorted(
        str(r.rel_path)
        for r in env.rows(export.export_record)
        if r.state in ("written", "scanned", "resolved")
    )


def kavita_scans(w: Wired, title: str) -> int:
    """Kavita scans the folder of the series: it indexes what is on disk, then Shelfhold reads
    the arrival (the record is resolved to the Kavita series)."""
    rels = written(w.env)
    folder = f"{REMOTE_ROOT}/{rels[0].rsplit('/', 1)[0]}"
    w.fake.discover(folder, [w.remote(r) for r in rels])
    run(ArrivalPoller(w.env.engine, w.env.clock, w.events).poll(w.client, w.env.destination_id))
    return next(s["id"] for s in w.fake.series if s["folderPath"] == folder)


def open_gate(w: Wired) -> Any:
    """What the export job does before it plans: push the ages due, then read them back."""
    gate = SeriesAgeGate(w.env.engine, w.env.clock, w.events)
    facts = run(gate.open(w.client, w.env.destination_id))
    w.env.pipeline.bind_age_gate(facts)
    return facts


def export_round(w: Wired) -> list[str]:
    """One run of the export job over the destination: the gate, then the pipeline."""
    open_gate(w)
    report = w.env.pipeline.run(w.env.destination_id, job_id=new_ulid())
    return sorted(str(op.rel_path) for op in report.ops)


def visible_to_restricted(age_rating: int, locked: bool, *, ceiling: int) -> bool:
    """Kavita shows a series to an account restricted to `ceiling` (a Kavita `AgeRating`) without
    "include unknowns" when its rating is known and not above the restriction."""
    return locked and age_rating != UNKNOWN and age_rating <= ceiling
