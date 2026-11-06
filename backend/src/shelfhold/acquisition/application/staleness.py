"""Which followed series are silent (EF-SRC-026 c), read from the database.

The follow cycle announces a series once (`series.stale`); `shelfhold status` lists them as long
as they are silent. Both ask the same question here: no chapter discovered for `k` times the
observed cadence (default 3), or for 14 days when the series has none, for a series that is not
finished and that the user did not pause or mark completed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import sqlalchemy as sa

from shelfhold.acquisition.domain.freshness import DEFAULT_K, cadence, is_stale, stale_threshold
from shelfhold.infrastructure.schema import acquisition, catalog

__all__ = ["FINISHED_STATUSES", "NOT_CHECKED", "StaleSeries", "discoveries_of", "stale_series"]

FINISHED_STATUSES = ("completed", "cancelled")  # work status: nothing more is expected
NOT_CHECKED = ("completed", "paused")  # series_follow.check_override: the user said so


@dataclass(frozen=True)
class StaleSeries:
    edition_id: str
    title: str
    last_discovery: datetime
    threshold: timedelta  # the silence after which the series counts as stale


def discoveries_of(connection: sa.Connection, edition_id: str) -> list[datetime]:
    v, u = catalog.unit_variant, catalog.unit
    return list(
        connection.execute(
            sa.select(v.c.first_seen_at)
            .select_from(v.join(u, u.c.id == v.c.unit_id))
            .where(u.c.edition_id == edition_id, v.c.removed_at.is_(None))
        ).scalars()
    )


def stale_series(
    connection: sa.Connection,
    now: datetime,
    *,
    k: int = DEFAULT_K,
    plugin_id: str | None = None,
) -> list[StaleSeries]:
    """Active, followed, unfinished series that have been silent for too long."""
    b, si = acquisition.source_binding, acquisition.source_item
    f, w = acquisition.series_follow, catalog.work
    query = (
        sa.select(b.c.edition_id, w.c.title)
        .select_from(
            b.join(si, si.c.id == b.c.source_item_id)
            .join(f, f.c.edition_id == b.c.edition_id)
            .join(w, w.c.id == b.c.work_id)
        )
        .where(
            b.c.active,
            sa.or_(f.c.check_override.is_(None), f.c.check_override.notin_(NOT_CHECKED)),
            w.c.status.notin_(FINISHED_STATUSES),
        )
        .distinct()
        .order_by(b.c.edition_id)
    )
    if plugin_id is not None:
        query = query.where(si.c.plugin_id == plugin_id)
    found: list[StaleSeries] = []
    for row in connection.execute(query).all():
        seen = discoveries_of(connection, row.edition_id)
        if is_stale(seen, now, k):
            found.append(
                StaleSeries(row.edition_id, row.title, max(seen), stale_threshold(cadence(seen), k))
            )
    return found
