"""The storage and network dashboard (EF-INT-020): the space each shelf holds, the traffic each
source made, and nothing else.

The space comes from the registry of copies (`library.public.space_by_shelf`: sizes and counts,
never a path), the traffic from the Fetcher's counter (`traffic_counter`: a source, a UTC day, two
numbers, never an address). Names are those of the shelves and of the sources, which the
administrator chose; this module hands the route nothing that could be a path, a host or a key.
Reading it is the administrator's (the route decides before this is called).
"""

from dataclasses import dataclass, field
from datetime import date, timedelta

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.infrastructure.schema import export, platform
from shelfhold.library.public import space_by_shelf

__all__ = [
    "MAX_DAYS",
    "ShelfRow",
    "SourceTraffic",
    "StorageDashboard",
    "TrafficDay",
    "storage_dashboard",
]

MAX_DAYS = 90


@dataclass(frozen=True)
class ShelfRow:
    shelf_id: str | None  # None: what is filed on no shelf
    name: str | None
    bytes: int
    copies: int
    by_location: dict[str, int]


@dataclass(frozen=True)
class TrafficDay:
    day: date
    requests: int
    bytes_in: int


@dataclass(frozen=True)
class SourceTraffic:
    source_key: str
    requests: int
    bytes_in: int
    by_day: list[TrafficDay] = field(default_factory=list)


@dataclass(frozen=True)
class StorageDashboard:
    shelves: list[ShelfRow]
    days: int
    sources: list[SourceTraffic]


def storage_dashboard(engine: Engine, *, today: date, days: int) -> StorageDashboard:
    """`days`: the window of the traffic, today included (1: today only)."""
    since = today - timedelta(days=days - 1)
    with engine.connect() as connection:
        names = dict(connection.execute(sa.select(export.shelf.c.id, export.shelf.c.name)).all())
        spaces = {space.shelf_id: space for space in space_by_shelf(connection)}
        counter = platform.traffic_counter
        traffic = connection.execute(
            sa.select(counter.c.source_key, counter.c.day, counter.c.requests, counter.c.bytes_in)
            .where(counter.c.day >= since, counter.c.day <= today)
            .order_by(counter.c.source_key, counter.c.day)
        ).all()
    shelves = [
        ShelfRow(
            shelf_id, name, ((s := spaces.get(shelf_id)) and s.bytes) or 0,
            s.copies if s else 0, s.by_location if s else {},
        )
        for shelf_id, name in sorted(names.items(), key=lambda item: (item[1].casefold(), item[0]))
    ]  # fmt: skip
    if None in spaces:
        loose = spaces[None]
        shelves.append(ShelfRow(None, None, loose.bytes, loose.copies, loose.by_location))
    grouped: dict[str, list[TrafficDay]] = {}
    for row in traffic:
        grouped.setdefault(row.source_key, []).append(
            TrafficDay(row.day, int(row.requests), int(row.bytes_in))
        )
    sources = [
        SourceTraffic(key, sum(d.requests for d in rows), sum(d.bytes_in for d in rows), rows)
        for key, rows in grouped.items()
    ]
    return StorageDashboard(shelves, days, sources)
