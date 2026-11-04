"""Idempotency-Key ledger on the `idempotency_key` table (EF-INT-001).

A key is scoped to its actor. The first request claims it; the stored response is replayed for
24 hours after completion. A claim that never completes (crash) frees itself after a short lease
instead of blocking the key for a day.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine, exc

from shelfhold.infrastructure.schema import identity

IDEMPOTENCY_TTL = timedelta(hours=24)
IN_FLIGHT_LEASE = timedelta(minutes=5)


@dataclass(frozen=True)
class Claim:
    actor_id: str
    key: str
    request_hash: str
    claimed_at: datetime


@dataclass(frozen=True)
class Fresh:
    """The key is claimed by this request: run the handler, then `complete` or `release`.

    `claim` is the reservation token: a lease that expired may be re-granted to another request,
    and the first one must then not overwrite the new owner's row.
    """

    claim: Claim


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
                return Fresh(Claim(actor_id, key, request_hash, now))
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
        self, claim: Claim, *, status: int, headers: Mapping[str, str], body: bytes
    ) -> bool:
        """Store the answer; False (and nothing written) when the claim was re-granted."""
        table = identity.idempotency_key
        envelope = {"headers": dict(headers), "body": json.loads(body) if body else None}
        with self._engine.begin() as connection:
            result = connection.execute(
                sa.update(table)
                .where(*self._owned(claim))
                .values(
                    response_status=status,
                    response_body=envelope,
                    expires_at=self._clock() + IDEMPOTENCY_TTL,
                )
            )
        if result.rowcount == 0:
            structlog.get_logger().warning("idempotency_claim_lost", key_hash=_short(claim))
            return False
        return True

    def release(self, claim: Claim) -> None:
        table = identity.idempotency_key
        with self._engine.begin() as connection:
            connection.execute(sa.delete(table).where(*self._owned(claim)))

    @staticmethod
    def _owned(claim: Claim) -> tuple[Any, ...]:
        table = identity.idempotency_key
        return (
            table.c.actor_id == claim.actor_id,
            table.c.key == claim.key,
            table.c.request_hash == claim.request_hash,
            table.c.created_at == claim.claimed_at,
            table.c.response_status.is_(None),
        )


def _short(claim: Claim) -> str:
    return hashlib.sha256(f"{claim.actor_id}\0{claim.key}".encode()).hexdigest()[:12]
