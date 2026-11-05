"""EF-EXP-018: "stop following this chapter", with its provenance, never lifted by itself."""

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import catalog


def suppress_follow(
    connection: Connection, unit_id: str, *, actor_id: str | None, now: datetime
) -> bool:
    """Mark the Unit; the flag survives a restore at the source and a new upload (a new Variant).

    Returns False when the Unit was already suppressed (the first provenance is kept).
    """
    unit = catalog.unit
    return bool(
        connection.execute(
            sa.update(unit)
            .where(unit.c.id == unit_id, sa.not_(unit.c.follow_suppressed))
            .values(follow_suppressed=True, follow_suppressed_by=actor_id, follow_suppressed_at=now)
        ).rowcount
    )
