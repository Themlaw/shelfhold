"""Download filters read from the database: what is refused before a request goes out
(EF-SRC-039). The rules are `domain.filters`; this reads the facts and keeps the reason of a
filtered Unit on the Unit, where the series' page shows it."""

from collections.abc import Collection

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.acquisition.domain.filters import (
    DownloadFilters,
    UnitFacts,
    parse_filters,
    refusal,
)
from shelfhold.infrastructure.schema import acquisition, catalog

__all__ = ["filters_of", "refusals", "set_filtered_reason"]

_UND = "und"


def filters_of(connection: Connection, edition_id: str) -> DownloadFilters:
    """The filters of the series (NULL: it inherits, and nothing refuses)."""
    f = acquisition.series_follow
    row = connection.execute(
        sa.select(f.c.filters).where(f.c.edition_id == edition_id)
    ).one_or_none()
    return parse_filters(None if row is None else row.filters)


def refusals(
    connection: Connection, edition_id: str, unit_id: str, variant_ids: Collection[str]
) -> dict[str, str]:
    """`{variant_id: reason}` for the offers of one Unit that the series' filters refuse."""
    filters = filters_of(connection, edition_id)
    if filters.is_open or not variant_ids:
        return {}
    u, v, t, e = catalog.unit, catalog.unit_variant, catalog.team, catalog.edition
    unit = connection.execute(
        sa.select(u.c.track, u.c.number_segment, e.c.lang.label("edition_lang"))
        .select_from(u.join(e, e.c.id == u.c.edition_id))
        .where(u.c.id == unit_id)
    ).one()
    rows = connection.execute(
        sa.select(v.c.id, v.c.lang, t.c.label)
        .select_from(v.outerjoin(t, t.c.id == v.c.team_id))
        .where(v.c.id.in_(set(variant_ids)))
    )
    out: dict[str, str] = {}
    for row in rows:
        lang = row.lang if row.lang and row.lang != _UND else unit.edition_lang or _UND
        reason = refusal(filters, UnitFacts(lang, row.label, unit.track, unit.number_segment))
        if reason is not None:
            out[row.id] = reason
    return out


def set_filtered_reason(connection: Connection, unit_id: str, reason: str | None) -> None:
    u = catalog.unit
    connection.execute(
        sa.update(u)
        .where(u.c.id == unit_id, u.c.filtered_reason.is_distinct_from(reason))
        .values(filtered_reason=reason)
    )
