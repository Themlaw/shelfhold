"""The release calendar of a followed series, kept from a metadata provider (EF-BIB-013).

The provider says when the series last released (MangaUpdates). The answer is cached in
`provider_release_calendar` for `CALENDAR_TTL`: asking again within it costs no request, and
`series.stale` reads the table, never the network. Nothing is read by anyone without the scope of
a reader: an Edition that the ceiling of a profile hides is not in what she gets, and asking for it
by its identifier is the same as asking for an unknown one. The provider's attribution and address
are kept with the row and shown wherever the calendar is.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Final, Literal

import sqlalchemy as sa
from shelfhold_sdk.metadata import EntityRef, MatchHints, MetadataProvider
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.catalog.application.queries import ReaderScope, visible_editions_clause
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog

__all__ = ["CALENDAR_TTL", "CalendarRefresh", "ReleaseCalendar", "ReleaseEntry"]

CALENDAR_TTL: Final = timedelta(hours=12)


@dataclass(frozen=True)
class ReleaseEntry:
    edition_id: str
    title: str
    provider: str
    last_release_on: date | None
    chapter: str | None
    volume: str | None
    source_url: str
    attribution: str  # always shown with the entry (the provider's terms)
    fetched_at: datetime


@dataclass(frozen=True)
class CalendarRefresh:
    outcome: Literal["fetched", "cached", "not_found"]


class ReleaseCalendar:
    def __init__(self, engine: Engine, clock: Clock, *, ttl: timedelta = CALENDAR_TTL) -> None:
        self._engine = engine
        self._clock = clock
        self._ttl = ttl

    async def refresh(
        self, provider: MetadataProvider, fetcher: Any, *, edition_id: str, title: str
    ) -> CalendarRefresh:
        key = provider.manifest.key
        table = catalog.provider_release_calendar
        now = self._clock.now()
        with self._engine.connect() as connection:
            held = connection.execute(
                sa.select(table.c.fetched_at).where(
                    table.c.provider == key, table.c.edition_id == edition_id
                )
            ).scalar()
        if held is not None and now - held < self._ttl:
            return CalendarRefresh("cached")  # no request: the answer is still good
        target = EntityRef(entity_type="edition", entity_id=edition_id)
        result = await provider.identify(fetcher, target, MatchHints(title=title))
        release = next((r for r in result.relations if r.kind == "release"
                        and r.provenance.provider == key), None)  # fmt: skip
        if release is None:
            return CalendarRefresh("not_found")
        data, source = release.data, release.provenance
        try:
            released = date.fromisoformat(str(data.get("released_on")))
        except ValueError:
            released = None
        values = {
            "remote_id": str(data.get("series_id", "")),
            "last_release_on": released,
            "chapter": _text(data.get("chapter")),
            "volume": _text(data.get("volume")),
            "source_url": source.source_url or provider.manifest.homepage or "",
            "attribution": source.attribution or provider.manifest.attribution or key,
            "fetched_at": now,
        }
        with write_transaction(self._engine) as connection:
            connection.execute(
                sqlite_insert(table)
                .values(provider=key, edition_id=edition_id, **values)
                .on_conflict_do_update(index_elements=[table.c.provider, table.c.edition_id],
                                       set_=values)
            )  # fmt: skip
        return CalendarRefresh("fetched")

    def entries(self, scope: ReaderScope, *, edition_id: str | None = None) -> list[ReleaseEntry]:
        """The calendar a reader may see: one filter, in the query, the one of the shelves."""
        cal, edition, work = catalog.provider_release_calendar.c, catalog.edition.c, catalog.work.c
        query = (
            sa.select(cal.edition_id, work.title, cal.provider, cal.last_release_on, cal.chapter,
                      cal.volume, cal.source_url, cal.attribution, cal.fetched_at)
            .select_from(
                catalog.provider_release_calendar
                .join(catalog.edition, edition.id == cal.edition_id)
                .join(catalog.work, work.id == edition.work_id)
            )
            .where(visible_editions_clause(scope))
            .order_by(cal.last_release_on.desc(), cal.edition_id, cal.provider)
        )  # fmt: skip
        if edition_id is not None:
            query = query.where(cal.edition_id == edition_id)
        with self._engine.connect() as connection:
            return [ReleaseEntry(**dict(row)) for row in connection.execute(query).mappings()]


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None
