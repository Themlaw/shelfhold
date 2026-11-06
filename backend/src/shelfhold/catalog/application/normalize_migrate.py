"""`shelfhold normalize migrate <normalizer>`: the explicit migration of ENF-OPS-021, with the
tombstone check of EF-BIB-054 (4).

A new version of a normalizer never recomputes anything on read: rows stay under the version that
made them (`*_norm_by`) until an admin migrates them. The migration has two steps. The **plan**
reads and writes nothing: for every row under an older version it computes the value under the
current one and sorts the row into one of four outcomes.

- `migrate`: the value is accepted and lands on a free place (it may be the same value; only the
  version moves);
- `merge`: the migrated value would land on a place another row holds, or two rows would land on
  the same one: a decision for a person, so the row stays as it is and the plan lists the group
  (a row whose value does not change never collides: only its version moves);
- `blocked`: the row, once migrated, would match a tombstone. It stays as it is and
  `tombstone.migration_hit` is emitted; the way out is `tombstone lift` or a purge (EF-BIB-054);
- `unresolved`: the target version refuses the raw value, or the change would turn a numbered Unit
  into an unnumbered one (or the reverse), which changes its track and is not a renaming.

The **apply** step runs only for the fingerprint of a plan the admin saw, recomputed at that
moment, and changes only the `migrate` rows, in one transaction with the audit entry. Nothing is
ever deleted, merged or moved to another place here.

Only the `number` normalizer is migratable in M1a: it is the one whose key is the Unit's place.
"""

import hashlib
import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.domain.normalizers import NormalizerId, NormalizerRegistry
from shelfhold.catalog.domain.numbering import NUMBER_NORMALIZER_NAME
from shelfhold.infrastructure.schema import catalog

MIGRATABLE = (NUMBER_NORMALIZER_NAME,)
LEGACY_VERSION = 1  # a row stored without `*_norm_by` was made by the first version


class Outcome(StrEnum):
    MIGRATE = "migrate"
    MERGE = "merge"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


class UnknownNormalizerError(ValueError):
    """No such normalizer, or one that cannot be migrated yet."""


class StalePlanError(RuntimeError):
    """The fingerprint is not the one of the plan as it stands now: look again."""


@dataclass(frozen=True)
class Candidate:
    """A live row as a tombstone sees it: its place with the raw values, in its Edition."""

    row_id: str
    edition_id: str
    track: str
    number_segment: str
    number_raw: str
    part: str


@dataclass(frozen=True)
class _Row:
    id: str
    edition_id: str
    track: str
    number_segment: str
    number_raw: str
    number_norm: str
    number_norm_by: str | None
    part: str


Screen = Callable[[Sequence[Candidate]], set[str]]
"""The tombstone check: the ids of the candidates that would match a tombstone (and the event)."""


@dataclass(frozen=True)
class PlannedRow:
    row_id: str
    outcome: Outcome
    old: str
    new: str  # the value under the target version ("" when unresolved)
    reason: str = ""  # a closed code, never text from the row


@dataclass(frozen=True)
class MigrationPlan:
    name: str
    target: NormalizerId
    rows: tuple[PlannedRow, ...]
    collisions: tuple[tuple[str, ...], ...]  # the row ids that would share one place
    fingerprint: str

    def of(self, outcome: Outcome) -> tuple[PlannedRow, ...]:
        return tuple(r for r in self.rows if r.outcome is outcome)

    @property
    def up_to_date(self) -> bool:
        return not self.rows


def _fingerprint(
    name: str,
    target: NormalizerId,
    rows: Sequence[PlannedRow],
    collisions: Sequence[Sequence[str]],
) -> str:
    document = [[r.row_id, r.outcome.value, r.new, r.reason] for r in rows]
    raw = json.dumps([name, str(target), document, collisions], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def plan_number_migration(
    connection: Connection, registry: NormalizerRegistry, screen: Screen
) -> MigrationPlan:
    """The plan for `number`; it reads, and asks `screen` about the rows that would move."""
    name = NUMBER_NORMALIZER_NAME
    target = registry.current(name)
    convert = registry.get(target)
    u = catalog.unit
    rows = [_Row(*r) for r in connection.execute(
        sa.select(
            u.c.id, u.c.edition_id, u.c.track, u.c.number_segment, u.c.number_raw, u.c.number_norm,
            u.c.number_norm_by, u.c.part,
        ).order_by(u.c.id)
    )]  # fmt: skip

    planned: dict[str, PlannedRow] = {}
    moving: dict[str, tuple[_Row, str]] = {}
    for row in rows:
        current = (
            NormalizerId.parse(row.number_norm_by)
            if row.number_norm_by
            else NormalizerId(name, LEGACY_VERSION)
        )
        if current >= target:
            continue  # up to date, or made by a version this build does not know: left alone
        try:
            new = convert(row.number_raw)
        except ValueError:
            planned[row.id] = PlannedRow(row.id, Outcome.UNRESOLVED, row.number_norm, "", "refused")
            continue
        if bool(new) != bool(row.number_norm):  # numbered <-> unnumbered: the track would change
            planned[row.id] = PlannedRow(
                row.id, Outcome.UNRESOLVED, row.number_norm, new, "changes_track"
            )
            continue
        moving[row.id] = (row, new)

    hits = screen(
        [
            Candidate(r.id, r.edition_id, r.track, r.number_segment, r.number_raw, r.part)
            for r, _new in moving.values()
        ]
    )
    for row_id in sorted(hits & moving.keys()):
        row, new = moving.pop(row_id)
        planned[row_id] = PlannedRow(row_id, Outcome.BLOCKED, row.number_norm, new, "tombstone")

    # who ends up where once every row that can move has moved (a numbered place is unique)
    landing: dict[tuple[str, str, str, str, str], list[str]] = defaultdict(list)
    for row in rows:
        norm = moving[row.id][1] if row.id in moving else row.number_norm
        if norm:
            landing[(row.edition_id, row.track, row.number_segment, row.part, norm)].append(row.id)
    for row_id, (row, new) in moving.items():
        place = (row.edition_id, row.track, row.number_segment, row.part, new)
        taken = new != row.number_norm and len(landing[place]) > 1
        planned[row_id] = (
            PlannedRow(row_id, Outcome.MERGE, row.number_norm, new, "same_place")
            if taken
            else PlannedRow(row_id, Outcome.MIGRATE, row.number_norm, new)
        )
    contested = {r for r in planned if planned[r].outcome is Outcome.MERGE}
    collisions = tuple(
        sorted(tuple(sorted(ids)) for ids in landing.values() if contested & set(ids))
    )
    ordered = tuple(sorted(planned.values(), key=lambda r: r.row_id))
    return MigrationPlan(
        name, target, ordered, collisions, _fingerprint(name, target, ordered, collisions)
    )


def apply_number_migration(
    connection: Connection,
    plan: MigrationPlan,
    *,
    audit: Callable[[Connection, MigrationPlan, int], None],
) -> int:
    """Move the `migrate` rows to the target version; `audit` writes in the same transaction.

    A row is touched only if it is still under the version the plan saw, so a plan applied twice,
    or after a row moved by other means, changes nothing the second time. Returns the count."""
    u = catalog.unit
    moved = 0
    for row in plan.of(Outcome.MIGRATE):
        result = connection.execute(
            sa.update(u)
            .where(u.c.id == row.row_id, u.c.number_norm == row.old,
                   sa.or_(u.c.number_norm_by.is_(None), u.c.number_norm_by != str(plan.target)))
            .values(
                number_norm=row.new,
                number_norm_by=str(plan.target),
                number_sort=float(row.new) if row.new else None,
            )
        )  # fmt: skip
        moved += int(result.rowcount)
    audit(connection, plan, moved)
    return moved
