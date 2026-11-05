"""Finding a series again in Kavita, whatever its current name (EF-EXP-057).

Kavita (0.9.1 and Kavita+) may rename a series. The name Shelfhold froze is therefore not an
identity: the series is looked up by the id it cached, then by the folder it lives in, and its
**current** name is read before anybody concludes to a collision (EF-EXP-032) or a match
(EF-EXP-038). A series renamed in the Kavita interface is the same series, not a second one, and
its name is not written over.
"""

from __future__ import annotations

from dataclasses import dataclass

from shelfhold.export.targets.kavita import dto
from shelfhold.export.targets.kavita.client import KavitaClient

__all__ = ["Resolution", "SeriesResolver", "folded"]


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
