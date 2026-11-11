"""The balance of the provider buckets, kept in `provider_quota` (EF-SRC-024, ENF-LEG-005).

`reserve` is one read-modify-write under `BEGIN IMMEDIATE`: the token and the day's count are
taken in the same transaction, so a second coroutine, Fetcher or process waits for the first and
reads its result. The count of the day is a row `<provider>:day:<YYYY-MM-DD>` of the same table
(`tokens` holds the count); rows of other days are dropped.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Engine, exc
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.fetcher.errors import ProviderQuotaExceeded
from shelfhold.infrastructure.schema import platform

__all__ = ["SqlBucketStore"]

_ATTEMPTS = 3


class SqlBucketStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def __eq__(self, other: object) -> bool:
        return isinstance(other, SqlBucketStore) and other._engine is self._engine

    def __hash__(self) -> int:
        return id(self._engine)

    def reserve(
        self, provider: str, rate: float, burst: int, daily_cap: int | None, now: float
    ) -> float:
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                with self._engine.connect() as connection:
                    connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                    connection.begin()
                    try:
                        wait = self._take(connection, provider, rate, burst, daily_cap, now)
                        connection.commit()
                    except BaseException:
                        connection.rollback()
                        raise
                    return wait
            except exc.OperationalError as error:
                busy = "locked" in str(error.orig).lower() or "busy" in str(error.orig).lower()
                if attempt == _ATTEMPTS or not busy:
                    raise
        raise AssertionError("unreachable")  # pragma: no cover

    @staticmethod
    def _take(
        connection: sa.Connection, provider: str, rate: float, burst: int,
        daily_cap: int | None, now: float,
    ) -> float:  # fmt: skip
        table = platform.provider_quota
        day_key = f"{provider}:day:{datetime.fromtimestamp(now, UTC).date().isoformat()}"
        if daily_cap is not None:
            used = connection.execute(
                sa.select(table.c.tokens).where(table.c.provider == day_key)
            ).scalar()
            if used is not None and used >= daily_cap:
                raise ProviderQuotaExceeded(f"the daily ceiling of {daily_cap} requests is reached")
        row = connection.execute(
            sa.select(table.c.tokens, table.c.refilled_at).where(table.c.provider == provider)
        ).first()
        if row is None:
            tokens = float(burst)
        else:
            at = row.refilled_at.replace(tzinfo=row.refilled_at.tzinfo or UTC).timestamp()
            tokens = min(float(burst), float(row.tokens) + (now - at) * rate)
        tokens -= 1.0
        wait = 0.0 if tokens >= 0 else -tokens / rate
        values = {
            "tokens": max(tokens, 0.0),
            "refilled_at": datetime.fromtimestamp(now + wait, UTC),
        }
        connection.execute(
            sqlite_insert(table)
            .values(provider=provider, **values)
            .on_conflict_do_update(index_elements=[table.c.provider], set_=values)
        )
        stale = sa.and_(table.c.provider.like(f"{provider}:day:%"), table.c.provider != day_key)
        connection.execute(sa.delete(table).where(stale))
        count = {"tokens": sa.func.coalesce(table.c.tokens, 0) + 1}
        connection.execute(
            sqlite_insert(table)
            .values(provider=day_key, tokens=1.0, refilled_at=datetime.fromtimestamp(now, UTC))
            .on_conflict_do_update(index_elements=[table.c.provider], set_=count)
        )
        return wait
