"""The balance of the provider buckets, kept in `provider_quota` (EF-SRC-024, ENF-LEG-005)."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.schema import platform

__all__ = ["SqlBucketStore"]


class SqlBucketStore:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def load(self, provider: str) -> tuple[float, float] | None:
        table = platform.provider_quota
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(table.c.tokens, table.c.refilled_at).where(table.c.provider == provider)
            ).first()
        if row is None:
            return None
        return float(row.tokens), row.refilled_at.replace(
            tzinfo=row.refilled_at.tzinfo or UTC
        ).timestamp()

    def save(self, provider: str, tokens: float, at: float) -> None:
        table = platform.provider_quota
        values = {"tokens": tokens, "refilled_at": datetime.fromtimestamp(at, UTC)}
        with self._engine.begin() as connection:
            connection.execute(
                sqlite_insert(table)
                .values(provider=provider, **values)
                .on_conflict_do_update(index_elements=[table.c.provider], set_=values)
            )
