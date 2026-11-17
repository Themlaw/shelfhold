"""The traffic counter, kept in `traffic_counter` (M4 L29, EF-INT-020).

One row per source and per UTC day: how many requests left the machine and how many bytes came
back. The row has no address, no host, no path and no query: `record` is not even handed one. The
Fetcher calls it after the network guard, so a request the guard refused is not traffic.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import platform

__all__ = ["SqlTrafficCounter"]


class SqlTrafficCounter:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(
        self, source_key: str, *, requests: int, bytes_in: int, now: datetime | None = None
    ) -> None:
        table = platform.traffic_counter
        day = (now or datetime.now(UTC)).astimezone(UTC).date()
        with write_transaction(self._engine) as connection:
            connection.execute(
                sqlite_insert(table)
                .values(day=day, source_key=source_key, requests=requests, bytes_in=bytes_in)
                .on_conflict_do_update(
                    index_elements=[table.c.day, table.c.source_key],
                    set_={
                        "requests": table.c.requests + requests,
                        "bytes_in": table.c.bytes_in + bytes_in,
                    },
                )
            )
