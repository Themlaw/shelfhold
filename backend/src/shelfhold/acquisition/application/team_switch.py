"""The preferred team of an Edition becomes the one exported (EF-BIB-005).

`prefer_team` (catalog) only orders the priority list. What is already served for a Unit of
another team is replaced by the same-path upgrade of the Unit, which runs under an intent with
every guard of its own (a read Unit, a suspect revision, an irreplaceable Unit, a frozen root).
The Unit follows the preferred team (`unit_variant.preferred`) only when that upgrade went
through, or when nothing is served for it yet: the flag and the served file never disagree. A
Unit whose preferred team has not fetched its release yet is left to the usual election.

The caller owns the transaction. Nothing is switched for a Variant that is removed or in a
numbering conflict.
"""

from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.acquisition.application.late_upgrade import Upgrader
from shelfhold.catalog.public import Catalog
from shelfhold.infrastructure.schema import acquisition, catalog, storage

__all__ = ["SwitchReport", "switch_to_preferred_team"]

_DONE = frozenset({"upgraded", "up_to_date"})


@dataclass(frozen=True)
class SwitchReport:
    switched: tuple[str, ...] = ()  # Units now exported from the preferred team
    blocked: dict[str, tuple[str, ...]] = field(default_factory=dict)  # Unit -> outcomes refused
    pending: tuple[str, ...] = ()  # Units whose preferred team has fetched nothing yet


def switch_to_preferred_team(
    connection: Connection, edition_id: str, *, upgrader: Upgrader
) -> SwitchReport:
    service = Catalog(connection)
    priority = service.team_priority(edition_id)
    if not priority:
        return SwitchReport()
    wanted = priority[0]
    u, v = catalog.unit, catalog.unit_variant
    switched: list[str] = []
    blocked: dict[str, tuple[str, ...]] = {}
    pending: list[str] = []
    units = connection.execute(
        sa.select(u.c.id)
        .select_from(u.join(v, sa.and_(v.c.unit_id == u.c.id, v.c.preferred)))
        .where(u.c.edition_id == edition_id, sa.func.coalesce(v.c.team_id, "") != wanted)
        .order_by(u.c.number_sort, u.c.id)
    ).scalars()
    for unit_id in list(units):
        live = [f for f in service.variant_facts(unit_id) if not f.removed and not f.in_conflict]
        mine = [f for f in live if f.team_id == wanted]
        content = _fetched_content(connection, [f.id for f in mine])
        if not content:
            if mine:
                pending.append(unit_id)
            continue
        variant_id, content_id = content
        outcomes = tuple(upgrader.upgrade_unit(unit_id, content_id))
        if all(o in _DONE for o in outcomes):
            service.set_preferred(unit_id, variant_id)
            switched.append(unit_id)
        else:
            blocked[unit_id] = outcomes
    return SwitchReport(tuple(switched), blocked, tuple(pending))


def _fetched_content(connection: Connection, variant_ids: list[str]) -> tuple[str, str] | None:
    """The oldest fetched cbz of one of the Variants: `(variant id, content id)`."""
    if not variant_ids:
        return None
    r, c, v = acquisition.download_request, storage.content, catalog.unit_variant
    row = connection.execute(
        sa.select(c.c.variant_id, c.c.id)
        .select_from(c.join(r, r.c.variant_id == c.c.variant_id).join(v, v.c.id == c.c.variant_id))
        .where(c.c.variant_id.in_(variant_ids), c.c.format == "cbz", r.c.state == "fetched")
        .order_by(v.c.first_seen_at, v.c.id, c.c.id)
        .limit(1)
    ).first()
    return None if row is None else (str(row[0]), str(row[1]))
