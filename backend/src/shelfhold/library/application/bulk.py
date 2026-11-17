"""The frozen selection of a bulk action (M4 L28, EF-BIB-023).

A bulk action is two steps and one job. The preview *freezes* what would be done into an order:
the identifiers of the Works chosen that wait, the parameters (the age), a digest of both. Applying
it names the order and nothing else, so no list of Works can be slipped into the apply: whoever
holds an order id still moves exactly the Works the preview froze, and only if they are still what
the preview saw (the job checks that again under the write lock).

An order belongs to the user who previewed it. For anyone else, a forged identifier, an expired
preview and an order that does not exist are the same absence: nothing tells them apart.

This module knows no rule of rating and no list of what waits; those stay with the caller, which
gives the Works. It reads and writes `bulk_order` only.
"""

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog

__all__ = [
    "KEEP_ENDED",
    "MAX_SELECTION",
    "ORDER_TTL",
    "Order",
    "attach",
    "claim",
    "close",
    "digest_of",
    "drop_dead_orders",
    "freeze",
    "load",
    "load_for_job",
    "progress",
    "release",
    "token_of",
]

MAX_SELECTION: Final = 1000  # Works one order may hold: a bound on what one job rewrites
ORDER_TTL: Final = timedelta(minutes=30)  # a preview that is not applied by then is not an order
KEEP_ENDED: Final = timedelta(days=30)  # an ended order is kept this long, for the audit trail

_ORDER = catalog.bulk_order


@dataclass(frozen=True)
class Order:
    id: str
    kind: str
    actor_id: str
    params: dict[str, Any]
    work_ids: tuple[str, ...]
    digest: str
    state: str
    job_id: str | None
    summary: dict[str, Any]


def digest_of(kind: str, params: dict[str, Any], facts: Iterable[str]) -> str:
    """What the order is: the kind, its parameters, and a fact per Work (its identifier and the
    state it was in: rated or not, at which age). The order of the facts is not part of it, a
    change of one fact is."""
    body = json.dumps([kind, params, sorted(set(facts))], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode()).hexdigest()


def token_of(digest: str, count: int) -> str:
    """What the screen gives back to apply: how many, and the start of the digest."""
    return f"{count}-{digest[:8]}"


def freeze(
    connection: Connection, *, kind: str, actor_id: str, params: dict[str, Any],
    work_ids: Sequence[str], facts: Sequence[str], now: datetime,
) -> Order:  # fmt: skip
    """Write an order for what the preview saw; `previewed` until it is applied. `facts` is what
    the digest covers (see `digest_of`): the job recomputes it under the write lock. Orders that
    can no longer be applied are dropped here, at the next insertion (`drop_dead_orders`)."""
    drop_dead_orders(connection, now=now)
    ids = tuple(sorted(set(work_ids)))
    order = Order(
        new_ulid(),
        kind,
        actor_id,
        params,
        ids,
        digest_of(kind, params, facts),
        "previewed",
        None,
        {},
    )
    connection.execute(
        sa.insert(_ORDER).values(
            id=order.id,
            kind=kind,
            actor_id=actor_id,
            params=params,
            work_ids=list(ids),
            digest=order.digest,
            state="previewed",
            created_at=now,
        )
    )
    return order


def drop_dead_orders(connection: Connection, *, now: datetime) -> int:
    """Drop what is of no use any more: a preview older than `ORDER_TTL` that nobody applied, and
    an order that ended (`applied`, `stale`, `refused`) more than `KEEP_ENDED` ago. An order that is
    queued is never dropped: its job reads it."""
    gone = connection.execute(
        sa.delete(_ORDER).where(
            sa.or_(
                sa.and_(_ORDER.c.state == "previewed", _ORDER.c.created_at < now - ORDER_TTL),
                sa.and_(
                    _ORDER.c.state.in_(("applied", "stale", "refused")),
                    _ORDER.c.applied_at < now - KEEP_ENDED,
                ),
            )
        )
    )
    return int(gone.rowcount)


def _of(row: Any) -> Order:
    return Order(
        row.id,
        row.kind,
        row.actor_id,
        dict(row.params),
        tuple(row.work_ids),
        row.digest,
        row.state,
        row.job_id,
        dict(row.summary or {}),
    )


def load(connection: Connection, order_id: str, *, actor_id: str, now: datetime) -> Order | None:
    """The order of this user that can still be applied, or None: absent, someone else's (IDOR),
    forged, or a preview older than `ORDER_TTL` are all None. An order already queued or applied
    is returned, so that the caller can say it was."""
    row = connection.execute(
        sa.select(_ORDER).where(_ORDER.c.id == order_id, _ORDER.c.actor_id == actor_id)
    ).first()
    if row is None:
        return None
    if row.state == "previewed" and _aware(row.created_at) + ORDER_TTL < now:
        return None
    return _of(row)


def load_for_job(connection: Connection, order_id: str) -> Order | None:
    """The order the job was queued for, whoever it belongs to (the job acts for its owner)."""
    row = connection.execute(sa.select(_ORDER).where(_ORDER.c.id == order_id)).first()
    return None if row is None else _of(row)


def claim(connection: Connection, order_id: str) -> bool:
    """`previewed` -> `queued`, once: False when another apply took it first. The job is queued
    after, and `attach` says which one; `release` gives the order back if it could not be."""
    found = connection.execute(
        sa.update(_ORDER)
        .where(_ORDER.c.id == order_id, _ORDER.c.state == "previewed")
        .values(state="queued")
    )
    return bool(found.rowcount == 1)


def attach(connection: Connection, order_id: str, *, job_id: str) -> None:
    connection.execute(sa.update(_ORDER).where(_ORDER.c.id == order_id).values(job_id=job_id))


def release(connection: Connection, order_id: str) -> None:
    """The job could not be queued: the order is as the preview left it."""
    connection.execute(
        sa.update(_ORDER)
        .where(_ORDER.c.id == order_id, _ORDER.c.state == "queued", _ORDER.c.job_id.is_(None))
        .values(state="previewed")
    )


def close(
    connection: Connection, order_id: str, state: str, *, now: datetime,
    summary: dict[str, Any] | None = None,
) -> None:  # fmt: skip
    """End an order that was queued: `applied`, `stale` or `refused`. Only a queued order ends."""
    connection.execute(
        sa.update(_ORDER)
        .where(_ORDER.c.id == order_id, _ORDER.c.state == "queued")
        .values(state=state, applied_at=now, summary=summary or {})
    )


def progress(connection: Connection, order_id: str, summary: dict[str, Any]) -> None:
    """Where a queued order stands between two tranches (the cursor, what was rated so far)."""
    connection.execute(
        sa.update(_ORDER)
        .where(_ORDER.c.id == order_id, _ORDER.c.state == "queued")
        .values(summary=summary)
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=UTC)
