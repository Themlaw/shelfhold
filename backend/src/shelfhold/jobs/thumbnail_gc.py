"""The window jobs of the cover thumbnails (EF-BIB-020, ENF-OPS-014): collect, then catch up.

`thumbnail_gc` drops the thumbnails of covers no Edition points at any more; `thumbnail_catchup`
makes the sizes a library that was never browsed still lacks, so that the first screen is not the
one that pays. Both run in the maintenance window only, stop between two files when it closes, and
work in `/data`: nothing of `/store` or `/library` is opened. The rules are in
`library.application.thumbnails`; this module wires them.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

import sqlalchemy as sa

from shelfhold.catalog.public import referenced_cover_refs
from shelfhold.composition import Composition
from shelfhold.infrastructure.imaging import WorkerLimits
from shelfhold.jobs.huey_app import JobContext, JobPools
from shelfhold.library.public import ThumbnailService, cover_hash

__all__ = [
    "THUMBNAIL_CATCHUP_KIND",
    "THUMBNAIL_GC_KIND",
    "register_thumbnail_jobs",
    "thumbnail_catchup_handler",
    "thumbnail_gc_handler",
]

THUMBNAIL_GC_KIND: Final = "thumbnail_gc"  # the name `WINDOW_JOB_ORDER` ranks
THUMBNAIL_CATCHUP_KIND: Final = "thumbnail_catchup"  # unranked: after every ranked window job
GC_CRON: Final = "20 4 * * *"
CATCHUP_CRON: Final = "40 4 * * *"


def _referenced(engine: sa.Engine) -> set[str]:
    with engine.connect() as connection:
        refs = referenced_cover_refs(connection)
    return {digest for ref in refs if (digest := cover_hash(ref)) is not None}


def _closing(context: JobContext) -> Callable[[], bool]:
    def closing() -> bool:
        over = context.window_end is not None and context.clock.now() >= context.window_end
        return over or context.stop_requested

    return closing


def thumbnail_gc_handler(
    comp: Composition, *, limits: WorkerLimits | None = None
) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        closing = _closing(context)
        service = ThumbnailService(
            comp.engine, comp.settings.data_dir, context.clock, limits=limits
        )
        done = service.collect(_referenced(comp.engine), closing=closing)
        context.preempted = closing()
        context.record(files=done.files, rows=done.rows)

    return run


def thumbnail_catchup_handler(
    comp: Composition, *, limits: WorkerLimits | None = None
) -> Callable[[JobContext], None]:
    def run(context: JobContext) -> None:
        closing = _closing(context)
        service = ThumbnailService(
            comp.engine, comp.settings.data_dir, context.clock, limits=limits
        )
        made = service.catch_up(_referenced(comp.engine), closing=closing)
        context.preempted = closing()
        context.record(made=made)

    return run


def register_thumbnail_jobs(pools: JobPools, comp: Composition) -> None:
    pools.register(
        THUMBNAIL_GC_KIND, pool="maint", handler=thumbnail_gc_handler(comp), windowed=True
    )
    pools.register(
        THUMBNAIL_CATCHUP_KIND, pool="maint", handler=thumbnail_catchup_handler(comp), windowed=True
    )
    pools.scheduler.add(THUMBNAIL_GC_KIND, GC_CRON, jitter_s=120, windowed=True)
    pools.scheduler.add(THUMBNAIL_CATCHUP_KIND, CATCHUP_CRON, jitter_s=120, windowed=True)
