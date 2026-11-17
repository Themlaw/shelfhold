"""The balance of the provider buckets, kept in `provider_quota` (EF-SRC-024, ENF-LEG-005).

`reserve` is one read-modify-write under `BEGIN IMMEDIATE`: the token and the day's count are
taken in the same transaction, so a second coroutine, Fetcher or process waits for the first and
reads its result. The count of the day is a row `<provider>:day:<YYYY-MM-DD>` of the same table
(`tokens` holds the count); rows of other days are dropped. The day is a day of UTC unless the
provider names a zone (`day_timezone`: Google's is the Pacific). A provider suspended for the rest
of its day is a row `<provider>:pause` whose `refilled_at` is the instant it may be asked again: no
token is given before it, for any worker.
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Engine, exc
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fetcher.day_window import day_key
from shelfhold.infrastructure.fetcher.errors import ProviderQuotaExceeded
from shelfhold.infrastructure.schema import platform
from shelfhold.sdk.metadata import AdaptiveLimit

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
        self, provider: str, rate: float, burst: int, daily_cap: int | None, now: float,
        adaptive: AdaptiveLimit | None = None, day_timezone: str | None = None,
    ) -> float:  # fmt: skip
        for attempt in range(1, _ATTEMPTS + 1):
            try:
                with self._engine.connect() as connection:
                    connection.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
                    connection.begin()
                    try:
                        wait = self._take(
                            connection,
                            provider,
                            rate,
                            burst,
                            daily_cap,
                            now,
                            adaptive,
                            day_timezone,
                        )
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
        daily_cap: int | None, now: float, adaptive: AdaptiveLimit | None = None,
        day_timezone: str | None = None,
    ) -> float:  # fmt: skip
        table = platform.provider_quota
        paused = SqlBucketStore._paused_until(connection, provider, now)
        if paused is not None:
            raise ProviderQuotaExceeded("the provider is suspended until its day is over")
        day_row = f"{provider}:day:{day_key(now, day_timezone)}"
        if daily_cap is not None:
            used = connection.execute(
                sa.select(table.c.tokens).where(table.c.provider == day_row)
            ).scalar()
            if used is not None and used >= daily_cap:
                raise ProviderQuotaExceeded(f"the daily ceiling of {daily_cap} requests is reached")
        row = connection.execute(
            sa.select(table.c.tokens, table.c.refilled_at, table.c.announced_limit).where(
                table.c.provider == provider
            )
        ).first()
        if adaptive is not None:  # the rate every worker last heard, read with the token it takes
            heard = row.announced_limit if row is not None else None
            rate = min(adaptive.max_per_minute, heard or adaptive.start_per_minute) / 60
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
        stale = sa.and_(table.c.provider.like(f"{provider}:day:%"), table.c.provider != day_row)
        connection.execute(sa.delete(table).where(stale))
        count = {"tokens": sa.func.coalesce(table.c.tokens, 0) + 1}
        connection.execute(
            sqlite_insert(table)
            .values(provider=day_row, tokens=1.0, refilled_at=datetime.fromtimestamp(now, UTC))
            .on_conflict_do_update(index_elements=[table.c.provider], set_=count)
        )
        return wait

    # -- what a response said (the rate it announced, the wait it asked for) ------------------
    def announce(self, provider: str, limit: int) -> None:
        table = platform.provider_quota
        with write_transaction(self._engine) as connection:
            connection.execute(
                sqlite_insert(table)
                .values(provider=provider, tokens=0.0, refilled_at=datetime.now(UTC),
                        announced_limit=limit)
                .on_conflict_do_update(index_elements=[table.c.provider],
                                       set_={"announced_limit": limit})
            )  # fmt: skip

    def defer(self, provider: str, seconds: float, now: float) -> None:
        """No token before `seconds` from `now`, for every worker."""
        table = platform.provider_quota
        until = datetime.fromtimestamp(now + seconds, UTC)
        with write_transaction(self._engine) as connection:
            connection.execute(
                sqlite_insert(table)
                .values(provider=provider, tokens=0.0, refilled_at=until)
                .on_conflict_do_update(index_elements=[table.c.provider],
                                       set_={"tokens": 0.0, "refilled_at": until})
            )  # fmt: skip

    # -- a provider suspended for the rest of its day (Google's 429 «Queries per day») ----------
    @staticmethod
    def _paused_until(connection: sa.Connection, provider: str, now: float) -> float | None:
        table = platform.provider_quota
        row = connection.execute(
            sa.select(table.c.refilled_at).where(table.c.provider == f"{provider}:pause")
        ).first()
        if row is None:
            return None
        at = row.refilled_at.replace(tzinfo=row.refilled_at.tzinfo or UTC).timestamp()
        return at if at > now else None

    def suspend(self, provider: str, until: float) -> None:
        """No request to `provider` before `until`, for every worker and every process."""
        table = platform.provider_quota
        instant = datetime.fromtimestamp(until, UTC)
        with write_transaction(self._engine) as connection:
            connection.execute(
                sqlite_insert(table)
                .values(provider=f"{provider}:pause", tokens=0.0, refilled_at=instant)
                .on_conflict_do_update(index_elements=[table.c.provider],
                                       set_={"refilled_at": instant})
            )  # fmt: skip

    def suspended_until(self, provider: str, now: float) -> float | None:
        with self._engine.connect() as connection:
            return self._paused_until(connection, provider, now)
