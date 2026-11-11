"""A better-ranked binding that lists a Unit late (EF-SRC-037, with EF-EXP-016).

Under `series_follow.upgrade_policy = prefer_rank` the Unit is asked of the better binding too,
and the file served at its path is replaced through the same-path upgrade, which keeps every
guard of its own: a read Unit, a suspect revision, an irreplaceable Unit and a frozen root are
left as they are. Under `keep_first` (the default) nothing is asked and nothing changes.

Only a strictly better rank, of the same team (a translation never changes in the middle of a
series, EF-BIB-028), on a binding that is active, in the Edition's own Work and allowed this
cycle, and only for a Unit that has no other open request and was never asked of that Variant.
"""

from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy import Connection

__all__ = [
    "FetchedUpgrade",
    "UpgradeCandidate",
    "Upgrader",
    "fetched_upgrades",
    "upgrade_candidates",
]


class Upgrader(Protocol):
    """The same-path upgrade of a Unit's served files to one of its contents: the outcomes, one
    per served file (`export.SamePathUpgrade.upgrade_unit`)."""

    def upgrade_unit(self, unit_id: str, content_id: str) -> list[str]: ...


@dataclass(frozen=True)
class UpgradeCandidate:
    unit_id: str
    variant_id: str
    binding_id: str


@dataclass(frozen=True)
class FetchedUpgrade:
    unit_id: str
    variant_id: str
    content_id: str


_CANDIDATES = sa.text(
    """
    SELECT v.id AS variant_id, v.unit_id AS unit_id, v.source_binding_id AS binding_id
    FROM unit_variant v
    JOIN unit u ON u.id = v.unit_id
    JOIN source_binding cb ON cb.id = v.source_binding_id
         AND cb.edition_id = u.edition_id AND cb.active
    JOIN edition e ON e.id = u.edition_id AND e.work_id = cb.work_id
    JOIN series_follow f ON f.edition_id = u.edition_id AND f.upgrade_policy = 'prefer_rank'
    JOIN unit_variant h ON h.unit_id = u.id AND h.id <> v.id
    JOIN download_request hr ON hr.variant_id = h.id AND hr.state = 'fetched'
    JOIN source_binding hb ON hb.id = h.source_binding_id AND hb.edition_id = u.edition_id
    WHERE v.removed_at IS NULL AND v.source_binding_id IN :allowed
      AND hb.priority > cb.priority AND h.team_id IS v.team_id
      AND NOT EXISTS (SELECT 1 FROM download_request x WHERE x.variant_id = v.id)
      AND NOT EXISTS (SELECT 1 FROM download_request y
                      WHERE y.unit_id = u.id AND y.state IN ('requested', 'downloaded'))
      AND NOT EXISTS (SELECT 1 FROM download_request z
                      JOIN unit_variant zv ON zv.id = z.variant_id
                      JOIN source_binding zb ON zb.id = zv.source_binding_id
                      WHERE zv.unit_id = u.id AND z.state = 'fetched'
                        AND zb.priority <= cb.priority)
      AND NOT EXISTS (SELECT 1 FROM merge_suggestion m
                      WHERE m.entity_type = 'variant' AND m.state = 'open'
                        AND (m.a = v.id OR m.b = v.id))
    GROUP BY v.id
    ORDER BY u.number_sort, cb.priority, v.id
    """
).bindparams(sa.bindparam("allowed", expanding=True))

_FETCHED = sa.text(
    """
    SELECT DISTINCT v.unit_id AS unit_id, v.id AS variant_id, c.id AS content_id
    FROM download_request r
    JOIN unit_variant v ON v.id = r.variant_id
    JOIN source_binding cb ON cb.id = v.source_binding_id
    JOIN content c ON c.variant_id = v.id AND c.format = 'cbz'
    JOIN series_follow f ON f.edition_id = cb.edition_id AND f.upgrade_policy = 'prefer_rank'
    JOIN unit_variant h ON h.unit_id = v.unit_id AND h.id <> v.id AND h.preferred
    JOIN source_binding hb ON hb.id = h.source_binding_id AND hb.edition_id = cb.edition_id
    WHERE r.state = 'fetched' AND r.updated_at >= :since
      AND hb.priority > cb.priority AND h.team_id IS v.team_id
    ORDER BY v.unit_id, v.id
    """
)


def upgrade_candidates(connection: Connection, allowed: Collection[str]) -> list[UpgradeCandidate]:
    """The Variants to ask for as an upgrade, one per Unit (the best rank), oldest Unit first."""
    if not allowed:
        return []
    seen: set[str] = set()
    found: list[UpgradeCandidate] = []
    for row in connection.execute(_CANDIDATES, {"allowed": sorted(allowed)}):
        if row.unit_id in seen:
            continue
        seen.add(row.unit_id)
        found.append(UpgradeCandidate(row.unit_id, row.variant_id, row.binding_id))
    return found


def fetched_upgrades(connection: Connection, since: datetime) -> list[FetchedUpgrade]:
    """The better-ranked copies received since `since` of a Unit whose exported Variant is of a
    worse rank: what the same-path upgrade is asked for."""
    return [
        FetchedUpgrade(row.unit_id, row.variant_id, row.content_id)
        for row in connection.execute(_FETCHED, {"since": since})
    ]
