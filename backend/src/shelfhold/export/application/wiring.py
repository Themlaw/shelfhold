"""The pipeline over a real instance: the database, the physical roots, `plugin/parse` replies.

For planning (`shelfhold export --dry-run`) and for the API. Fsops covers the physical roots as
registered in `physical_root` (as at boot). `plugin/parse` answers come from Kavita through
`parse-bulk`; until the destination is connected here (L29), the names are not checked remotely
and the preview says so and the apply refuses (`UnverifiedParseError`, `bind_parse(verified=...)`).
"""

from collections.abc import Callable
from pathlib import Path

import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.export.application.pipeline import ExportPipeline
from shelfhold.export.application.roots import load_roots, open_fsops
from shelfhold.export.domain.naming import Parsed
from shelfhold.export.domain.path_map import PathMap
from shelfhold.infrastructure.clock import SystemClock
from shelfhold.infrastructure.db import create_db_engine
from shelfhold.infrastructure.schema import platform

__all__ = ["open_pipeline"]


class NoRemoteParse:
    """Stands for `plugin/parse` when no Kavita is connected: reads no volume, no chapter."""

    min_chapter = 0.0
    min_volume = 0.0


def _unchecked(_name: str) -> Parsed:
    return NoRemoteParse()


def open_pipeline(
    settings: Settings,
    *,
    write_gate: Callable[[], None] | None = None,
    stop_gate: Callable[[], None] | None = None,
    gate: Callable[[], None] | None = None,
) -> ExportPipeline:
    engine = create_db_engine(settings.database_path)
    with engine.connect() as connection:
        paths = [r.path_entry for r in connection.execute(sa.select(platform.physical_root))]
        loaded = load_roots(connection, PathMap.same_path(paths))
    fsops = open_fsops(loaded, umask=settings.umask, write_gate=write_gate, stop_gate=stop_gate)

    return ExportPipeline(
        engine, fsops, locate=loaded.locate, data_dir=Path(settings.data_dir), clock=SystemClock(),
        parse=_unchecked,
        parse_verified=False,
        segment_policy_confirmed=settings.naming_segment_policy_confirmed,
        gate=gate,
    )  # fmt: skip
