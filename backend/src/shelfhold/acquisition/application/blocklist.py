"""The blocklist (EF-INT-019): chapters a source must not bring back.

A chapter is named by what its source calls it, `(source_key, remote_unit_id)`: the block holds
whether or not a Unit, a binding or the series still exists here. The follow cycle asks
`blocked_ids` for the source it is looking at and ignores what is listed there; each refusal is
counted (`note_reproposed`), so the history can say how often a source insisted.

Only `unblock` takes a row away, and only the administrator's route calls it, with its audit line.
Nothing here expires, and nothing a cycle does lifts a block: "never unblocks in silence".
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition

__all__ = [
    "BLOCK_REASONS",
    "Block",
    "block_unit",
    "blocked_ids",
    "find_block",
    "get_block",
    "list_blocks",
    "note_reproposed",
    "unblock",
]

BLOCK_REASONS = acquisition.BLOCK_REASONS
_TABLE = acquisition.activity_block


@dataclass(frozen=True)
class Block:
    id: str
    source_key: str
    remote_unit_id: str
    reason: str
    created_by: str | None
    created_at: datetime
    proposed_count: int
    last_proposed_at: datetime | None


def _block_of(row: sa.Row[tuple[object, ...]]) -> Block:
    return Block(
        id=row.id, source_key=row.source_key, remote_unit_id=row.remote_unit_id,
        reason=row.reason, created_by=row.created_by, created_at=row.created_at,
        proposed_count=row.proposed_count, last_proposed_at=row.last_proposed_at,
    )  # fmt: skip


def block_unit(
    connection: Connection,
    *,
    source_key: str,
    remote_unit_id: str,
    reason: str,
    actor_id: str | None,
    now: datetime,
) -> Block:
    """Block a chapter. One already blocked is returned as it is (the first reason stays)."""
    if reason not in BLOCK_REASONS:
        raise ValueError(f"reason must be one of {BLOCK_REASONS}")
    if not source_key or not remote_unit_id:
        raise ValueError("a block names a source and a chapter")
    connection.execute(
        sqlite_insert(_TABLE)
        .values(
            id=new_ulid(), source_key=source_key, remote_unit_id=remote_unit_id,
            reason=reason, created_by=actor_id, created_at=now,
        )
        .on_conflict_do_nothing(index_elements=[_TABLE.c.source_key, _TABLE.c.remote_unit_id])
    )  # fmt: skip
    row = connection.execute(
        sa.select(_TABLE).where(
            _TABLE.c.source_key == source_key, _TABLE.c.remote_unit_id == remote_unit_id
        )
    ).one()
    return _block_of(row)


def unblock(connection: Connection, block_id: str) -> bool:
    """The one way out. False: there was nothing to lift."""
    return connection.execute(sa.delete(_TABLE).where(_TABLE.c.id == block_id)).rowcount > 0


def get_block(connection: Connection, block_id: str) -> Block | None:
    row = connection.execute(sa.select(_TABLE).where(_TABLE.c.id == block_id)).first()
    return _block_of(row) if row is not None else None


def find_block(connection: Connection, source_key: str, remote_unit_id: str) -> Block | None:
    row = connection.execute(
        sa.select(_TABLE).where(
            _TABLE.c.source_key == source_key, _TABLE.c.remote_unit_id == remote_unit_id
        )
    ).first()
    return _block_of(row) if row is not None else None


def list_blocks(connection: Connection) -> list[Block]:
    rows = connection.execute(sa.select(_TABLE).order_by(_TABLE.c.created_at.desc(), _TABLE.c.id))
    return [_block_of(row) for row in rows]


def blocked_ids(connection: Connection, source_key: str) -> frozenset[str]:
    """The chapters of one source that are blocked: one cheap read per series per cycle."""
    return frozenset(
        connection.execute(
            sa.select(_TABLE.c.remote_unit_id).where(_TABLE.c.source_key == source_key)
        ).scalars()
    )


def note_reproposed(
    connection: Connection, source_key: str, remote_unit_ids: Collection[str], now: datetime
) -> None:
    """The source offered these blocked chapters again and they were ignored."""
    if not remote_unit_ids:
        return
    connection.execute(
        sa.update(_TABLE)
        .where(
            _TABLE.c.source_key == source_key, _TABLE.c.remote_unit_id.in_(list(remote_unit_ids))
        )
        .values(proposed_count=_TABLE.c.proposed_count + 1, last_proposed_at=now)
    )
