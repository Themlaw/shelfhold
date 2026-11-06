"""Finding a series again in Kavita, whatever its current name (EF-EXP-057).

Kavita (0.9.1 and Kavita+) may rename a series. The name Shelfhold froze is therefore not an
identity: the series is looked up by the id it cached, then by the folder it lives in, and its
**current** name is read before anybody concludes to a collision (EF-EXP-032) or a match
(EF-EXP-038). A series renamed in the Kavita interface is the same series, not a second one, and
its name is not written over.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from shelfhold.export.application.snapshot import load_context
from shelfhold.export.domain.naming import normalize_series_name
from shelfhold.export.targets.kavita import dto
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.infrastructure.schema import export

__all__ = ["Resolution", "SeriesResolver", "folded", "refresh_names"]


def folded(name: str) -> str:
    return " ".join(name.casefold().split())


@dataclass(frozen=True)
class Resolution:
    series: dto.SeriesInfo | None
    by: str | None  # "cache" | "path" | None

    @property
    def found(self) -> bool:
        return self.series is not None

    def renamed(self, frozen_name: str | None) -> bool:
        """Kavita shows another name than the one Shelfhold froze (never rewritten)."""
        return (
            self.series is not None
            and frozen_name is not None
            and folded(self.series.name) != folded(frozen_name)
        )


class SeriesResolver:
    def __init__(self, client: KavitaClient) -> None:
        self._client = client

    async def resolve(
        self, *, library_id: int, remote_folder: str, cached_id: int | None = None
    ) -> Resolution:
        if cached_id is not None:
            cached = (await self._client.series_by_ids([cached_id])).get(cached_id)
            if cached is not None and cached.library_id == library_id:
                return Resolution(cached, "cache")
        for series in await self._client.series_of_library(library_id):
            if series.lives_in(remote_folder):
                return Resolution(series, "path")
        return Resolution(None, None)

    async def collision(
        self, *, library_id: int, planned_name: str, remote_folder: str, own: Resolution
    ) -> dto.SeriesInfo | None:
        """The other series of the library the planned name would merge into, if any.

        The current names are read, not the cache: our own series, renamed, never collides with
        its former name, and a series that now bears the planned name does.
        """
        own_id = own.series.id if own.series else None
        for series in await self._client.series_of_library(library_id):
            if series.id == own_id or series.lives_in(remote_folder):
                continue
            if folded(series.name) == folded(planned_name):
                return series
        return None


async def refresh_names(engine: Engine, client: KavitaClient, destination_id: str) -> int:
    """Read the current names of the destination's series in Kavita and keep them in the cache
    (`export_series.name_raw` / `normalized_name`, EF-EXP-057).

    Kavita (and Kavita+) may rename a series: the planner would then see a collision with a name
    nobody bears any more, or miss one that somebody now does, and the pairing would compare
    titles with a stale one. The frozen name (`frozen_series`, the files') is never touched, and
    nothing is written in Kavita. A cached id Kavita no longer knows is found again by the folder
    the series lives in. Returns the number of names that changed.
    """
    series = export.export_series
    with engine.connect() as connection:
        ctx = load_context(connection, destination_id)
        rows = connection.execute(
            sa.select(
                series.c.id,
                series.c.kavita_series_id,
                series.c.series_dir,
                series.c.name_raw,
                series.c.normalized_name,
            ).where(
                series.c.shelf_destination_id == destination_id,
                series.c.kavita_series_id.isnot(None),
            )
        ).all()
        library = connection.execute(
            sa.select(export.remote_library.c.remote_ref).where(
                export.remote_library.c.id == ctx.remote_library_id
            )
        ).scalar()
    if not rows or library is None or not str(library).isdigit():
        return 0
    current = await client.series_by_ids([int(r.kavita_series_id) for r in rows])
    root = ctx.remote_folder(ctx.root_local) if ctx.root_local is not None else None
    lost = [r for r in rows if int(r.kavita_series_id) not in current]
    if lost and root is not None:  # a reinstall renumbers: the folder is the other identity
        listed = await client.series_of_library(int(library))
        for row in lost:
            folder = posixpath.join(root, row.series_dir) if row.series_dir else None
            found = next((s for s in listed if folder and s.lives_in(folder)), None)
            if found is not None:
                current[int(row.kavita_series_id)] = found
    changed = 0
    for row in rows:
        now = current.get(int(row.kavita_series_id))
        if now is None or folded(now.name) == folded(row.name_raw):
            continue
        normalized = normalize_series_name(now.name)
        if not normalized:
            continue
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.update(series)
                    .where(series.c.id == row.id)
                    .values(name_raw=now.name, normalized_name=normalized)
                )
        except IntegrityError:  # another series of the library already bears it: left as it is
            continue
        changed += 1
    return changed
