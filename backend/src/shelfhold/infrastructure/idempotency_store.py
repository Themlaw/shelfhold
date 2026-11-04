"""Idempotency-Key ledger on the `idempotency_key` table (EF-INT-001).

A key is scoped to its actor. The first request claims it; the stored response is replayed for
24 hours after completion. A claim that never completes (crash) frees itself after a short lease
instead of blocking the key for a day.
"""

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine, exc

from shelfhold.infrastructure.schema import identity

IDEMPOTENCY_TTL = timedelta(hours=24)
IN_FLIGHT_LEASE = timedelta(minutes=5)


@dataclass(frozen=True)
class Fresh:
    """The key is claimed by this request: run the handler, then `complete` or `release`."""


@dataclass(frozen=True)
class Replay:
    status: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class Reused:
    """Same key, different request: a client bug, never a replay."""


@dataclass(frozen=True)
class InFlight:
    """The first request with this key has not finished."""


Outcome = Fresh | Replay | Reused | InFlight


def _now() -> datetime:
    return datetime.now(UTC)


class IdempotencyStore:
    def __init__(self, engine: Engine, *, clock: Callable[[], datetime] = _now) -> None:
        self._engine = engine
        self._clock = clock

    def begin(
        self, *, actor_id: str, key: str, method: str, path: str, request_hash: str
    ) -> Outcome:
        table = identity.idempotency_key
        now = self._clock()
        mine = sa.and_(table.c.actor_id == actor_id, table.c.key == key)
        with self._engine.begin() as connection:
            connection.execute(sa.delete(table).where(table.c.expires_at <= now))
            row = connection.execute(sa.select(table).where(mine)).mappings().first()
            if row is None:
                try:
                    connection.execute(
                        sa.insert(table).values(
                            actor_id=actor_id,
                            key=key,
                            method=method,
                            path=path,
                            request_hash=request_hash,
                            created_at=now,
                            expires_at=now + IN_FLIGHT_LEASE,
                        )
                    )
                except exc.IntegrityError:  # lost the race for the same key
                    return InFlight()
                return Fresh()
        if (row["method"], row["path"], row["request_hash"]) != (method, path, request_hash):
            return Reused()
        if row["response_status"] is None:
            return InFlight()
        envelope: dict[str, Any] = row["response_body"] or {}
        body = envelope.get("body")
        return Replay(
            status=row["response_status"],
            headers=envelope.get("headers", {}),
            body=b"" if body is None else json.dumps(body, ensure_ascii=False).encode(),
        )

    def complete(
        self, *, actor_id: str, key: str, status: int, headers: Mapping[str, str], body: bytes
    ) -> None:
        table = identity.idempotency_key
        envelope = {"headers": dict(headers), "body": json.loads(body) if body else None}
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(table)
                .where(table.c.actor_id == actor_id, table.c.key == key)
                .values(
                    response_status=status,
                    response_body=envelope,
                    expires_at=self._clock() + IDEMPOTENCY_TTL,
                )
            )

    def release(self, *, actor_id: str, key: str) -> None:
        table = identity.idempotency_key
        with self._engine.begin() as connection:
            connection.execute(
                sa.delete(table).where(
                    table.c.actor_id == actor_id,
                    table.c.key == key,
                    table.c.response_status.is_(None),
                )
            )
