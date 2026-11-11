"""Merging two Editions of a Work: a plan, a confirmation, never a silent recompute
(EF-BIB-049, EF-BIB-054 (4), EF-BIB-055).

`plan_edition_merge` reads and writes nothing. It says what moves from the Edition that goes (the
donor) into the Edition that stays (the survivor), and what stops the merge:

- **structure**: another Work; a place, a volume number or an upstream series that both hold;
  anything else that hangs from the donor (follow, export, team, attach journal...) and that this
  merge does not know how to move. The list of what hangs from an Edition is read from the schema,
  so a table that arrives later stops the merge until it is taught (fail closed);
- **tombstones**: a Unit that, in the survivor, would take the place of a purged Unit (`place`),
  or a Work whose external identifier is tombstoned (`work`). Nothing is deleted or moved, and the
  hit is told (`tombstone.migration_hit`, by the caller that owns the bus).

The **age** never goes down: the survivor takes the strictest of the two ages (the age of an
Edition that has none of its own is its Work's), a suggested age of the donor follows the Units,
and the survivor counts as unrated as soon as one of the two was: the new-Edition barrier
(EF-BIB-055) is not a door around the parental decision. The survivor goes through the filing gate
again, so a shelf below 18+ that no longer admits it lets it go at once.

`apply_edition_merge` acts for the fingerprint of a plan the admin saw, recomputed at that moment
in the transaction that writes; two merges that race see one another's result and the second is
stale. `plan_after_contributor_merge` is what a merge of contributors proposes (EF-BIB-049): the
plans of the Editions that would then share a key. It applies nothing.
"""

import hashlib
import hmac
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, exc

from shelfhold.catalog.application.new_edition_gate import Filing, file_new_edition
from shelfhold.catalog.application.tombstone_screen import MergedPlace, TombstoneScreen
from shelfhold.catalog.domain.external_ids import ExternalId
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.infrastructure.schema._base import metadata as _METADATA
from shelfhold.sdk.age import AgeRating, max_rating, to_columns

__all__ = [
    "EDITION_KEY",
    "EditionMergePlan",
    "EditionMergeRefusedError",
    "EditionMerged",
    "StaleMergePlanError",
    "apply_edition_merge",
    "plan_after_contributor_merge",
    "plan_edition_merge",
]

# The raw key a `place` tombstone cites: the purge records the same columns.
EDITION_KEY = (
    "work_id", "lang", "translation_type", "media", "publisher", "text_version", "performance",
    "packaging",
)  # fmt: skip
_MOVED = frozenset({"unit", "volume", "source_binding"})
_TWIN_FREE = ("work_id", "text_version")  # the Work is the same; the translator is what merges


class EditionMergeRefusedError(Exception):
    """The plan has blockers: nothing was changed. `plan` says which."""

    def __init__(self, plan: "EditionMergePlan") -> None:
        super().__init__("edition merge refused: " + ", ".join(plan.blockers))
        self.plan = plan


class StaleMergePlanError(Exception):
    """The plan is not the one of the data as it stands now: read it again (`plan.stale`)."""

    code = "plan.stale"


@dataclass(frozen=True)
class EditionMergePlan:
    keep_id: str
    drop_id: str
    work_id: str
    units: tuple[str, ...]  # moved from the donor to the survivor
    volumes: tuple[str, ...]
    bindings: tuple[str, ...]
    blockers: tuple[str, ...]  # structure: why this merge cannot be applied by this command
    tombstoned_units: tuple[str, ...]  # their place is covered by a `place` tombstone
    tombstoned_ids: int  # external identifiers of the Work a `work` tombstone covers (a count)
    keep_age: str | None  # `13+`, `X` or `None` (nobody rated)
    drop_age: str | None
    merged_age: str | None  # the strictest of the two: never lower than either
    unrated_after: bool  # one of the two was unrated: the survivor is, until an admin rates it
    shelf_id: str | None  # the survivor's shelf now (the donor's shelf is not carried over)
    fingerprint: str

    @property
    def mergeable(self) -> bool:
        return not self.blockers and not self.tombstoned_units and not self.tombstoned_ids

    @property
    def reasons(self) -> tuple[str, ...]:
        out = list(self.blockers)
        if self.tombstoned_units:
            out.append("tombstone:place")
        if self.tombstoned_ids:
            out.append("tombstone:work")
        return tuple(out)


@dataclass(frozen=True)
class EditionMerged:
    plan: EditionMergePlan
    filing: Filing  # where the survivor is filed after the gate


def _age(age_rating: int | None, age_x: bool) -> AgeRating | None:
    if age_x:
        return AgeRating(18, x=True)
    return None if age_rating is None else AgeRating(int(age_rating))


def _label(age: AgeRating | None) -> str | None:
    if age is None:
        return None
    return "X" if age.x else f"{age.min_age}+"


def _hanging() -> list[tuple[sa.Table, sa.Column[Any]]]:
    """Every table that points at an Edition and that this merge does not move."""
    found: list[tuple[sa.Table, sa.Column[Any]]] = []
    for table in _METADATA.sorted_tables:
        if table.name in _MOVED or table.name == "edition":
            continue
        for fk in table.foreign_keys:
            if fk.target_fullname == "edition.id":
                found.append((table, fk.parent))
    return found


def _edition(connection: Connection, edition_id: str) -> sa.Row[Any]:
    row = connection.execute(
        sa.select(catalog.edition).where(catalog.edition.c.id == edition_id)
    ).first()
    if row is None:
        raise LookupError(f"no such Edition: {edition_id}")
    return row


def _own_age(connection: Connection, row: sa.Row[Any]) -> AgeRating | None:
    """The age an Edition counts for: its own when an admin fixed one, else its Work's."""
    explicit = _age(row.age_rating, row.age_x)
    if explicit is not None:
        return explicit
    work, edition = catalog.work, catalog.edition
    own = connection.execute(
        sa.select(work.c.age_rating, work.c.age_x).where(work.c.id == row.work_id)
    ).one()
    others = connection.execute(
        sa.select(edition.c.age_rating, edition.c.age_x).where(edition.c.work_id == row.work_id)
    )
    return max_rating(
        a for a in (_age(own[0], own[1]), *(_age(r[0], r[1]) for r in others)) if a is not None
    )


def _places(connection: Connection, edition_id: str) -> list[sa.Row[Any]]:
    return list(
        connection.execute(sa.select(catalog.unit).where(catalog.unit.c.edition_id == edition_id))
    )


def _collisions(connection: Connection, keep_id: str, drop_id: str) -> list[str]:
    out: list[str] = []
    kept, moved = _places(connection, keep_id), _places(connection, drop_id)
    numbered = {(r.track, r.number_segment, r.number_norm, r.part) for r in kept}
    order = {r.source_order for r in kept if r.track == "unnumbered"}
    for r in moved:
        if r.track == "unnumbered":
            if r.source_order in order:
                out.append("collision:unit_order")
        elif (r.track, r.number_segment, r.number_norm, r.part) in numbered:
            out.append("collision:unit_place")
    v = catalog.volume.c
    have = set(connection.execute(sa.select(v.number).where(v.edition_id == keep_id)).scalars())
    moving = connection.execute(sa.select(v.number).where(v.edition_id == drop_id)).scalars()
    if any(n in have for n in moving):
        out.append("collision:volume_number")
    b = acquisition.source_binding.c
    keep_b = connection.execute(
        sa.select(b.source_item_id, b.priority, b.active).where(b.edition_id == keep_id)
    ).all()
    drop_b = connection.execute(
        sa.select(b.source_item_id, b.priority, b.active).where(b.edition_id == drop_id)
    ).all()
    items = {r.source_item_id for r in keep_b}
    ranks = {r.priority for r in keep_b if r.active}
    if any(r.source_item_id in items for r in drop_b):
        out.append("collision:binding_source")
    if any(r.active and r.priority in ranks for r in drop_b):
        out.append("collision:binding_rank")
    return sorted(set(out))


def plan_edition_merge(
    connection: Connection, keep_id: str, drop_id: str, *, screen: TombstoneScreen
) -> EditionMergePlan:
    """What merging `drop_id` into `keep_id` would do, and what stops it. Writes nothing.

    `LookupError` for an Edition that does not exist (a plan that moved under a racing merge),
    `ValueError` for an Edition merged into itself."""
    if keep_id == drop_id:
        raise ValueError("an Edition is not merged into itself")
    keep, drop = _edition(connection, keep_id), _edition(connection, drop_id)
    blockers: list[str] = []
    if keep.work_id != drop.work_id:
        blockers.append("different_work")
    for table, column in _hanging():
        found = connection.execute(
            sa.select(sa.literal(1)).select_from(table).where(column == drop_id).limit(1)
        ).first()
        if found is not None:
            blockers.append(f"dependent:{table.name}")
    pointed = connection.execute(
        sa.select(catalog.edition.c.id).where(
            catalog.edition.c.translated_from_edition_id == drop_id
        )
    ).first()
    if pointed is not None:
        blockers.append("dependent:edition")
    blockers.extend(_collisions(connection, keep_id, drop_id))

    units = _places(connection, drop_id)
    key = tuple((c, str(keep._mapping[c])) for c in EDITION_KEY)
    places = [
        MergedPlace(u.id, key, u.track, u.number_segment, u.number_raw, u.part) for u in units
    ]
    tombstoned_units = sorted(screen.places(places)) if places else []
    ids = [
        ExternalId("work", r.provider, r.value, r.id_scheme)
        for r in connection.execute(
            sa.select(catalog.external_id).where(
                catalog.external_id.c.entity_type == "work",
                catalog.external_id.c.entity_id == keep.work_id,
            )
        )
    ]
    tombstoned_ids = len(screen.work_ids(ids)) if ids else 0

    keep_age, drop_age = _own_age(connection, keep), _own_age(connection, drop)
    merged = max_rating(a for a in (keep_age, drop_age) if a is not None)
    unrated_after = keep.rated_at is None or drop.rated_at is None
    volumes = sorted(
        connection.execute(
            sa.select(catalog.volume.c.id).where(catalog.volume.c.edition_id == drop_id)
        ).scalars()
    )
    bindings = sorted(
        connection.execute(
            sa.select(acquisition.source_binding.c.id).where(
                acquisition.source_binding.c.edition_id == drop_id
            )
        ).scalars()
    )
    body: dict[str, Any] = {
        "keep": keep_id, "drop": drop_id, "units": sorted(u.id for u in units),
        "volumes": volumes, "bindings": bindings, "blockers": sorted(set(blockers)),
        "places": tombstoned_units, "ids": tombstoned_ids, "keep_age": _label(keep_age),
        "drop_age": _label(drop_age), "merged": _label(merged), "unrated": unrated_after,
        "shelf": keep.shelf_id,
    }  # fmt: skip
    digest = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:16]
    return EditionMergePlan(
        keep_id, drop_id, str(keep.work_id), tuple(sorted(u.id for u in units)), tuple(volumes),
        tuple(bindings), tuple(sorted(set(blockers))), tuple(tombstoned_units), tombstoned_ids,
        _label(keep_age), _label(drop_age), _label(merged), unrated_after, keep.shelf_id, digest,
    )  # fmt: skip


def apply_edition_merge(
    connection: Connection,
    keep_id: str,
    drop_id: str,
    *,
    confirm: str,
    screen: TombstoneScreen,
    audit: Callable[[Connection, EditionMergePlan], None] | None = None,
) -> EditionMerged:
    """Merge for the fingerprint of the plan the admin read, recomputed here. The caller holds the
    write lock (`BEGIN IMMEDIATE`) and the transaction: the plan and the writes see the same data.

    `StaleMergePlanError` when the plan is not the one of the data now (including a donor that a
    racing merge already took); `EditionMergeRefusedError` when it has blockers. In both cases
    nothing was written."""
    try:
        plan = plan_edition_merge(connection, keep_id, drop_id, screen=screen)
    except LookupError:
        raise StaleMergePlanError(drop_id) from None
    if not hmac.compare_digest(plan.fingerprint.encode(), confirm.strip().encode()):
        raise StaleMergePlanError(plan.fingerprint)
    if not plan.mergeable:
        raise EditionMergeRefusedError(plan)
    keep, drop = _edition(connection, keep_id), _edition(connection, drop_id)
    ages = [_own_age(connection, keep), _own_age(connection, drop)]
    merged = max_rating(a for a in ages if a is not None)
    savepoint = connection.begin_nested()
    try:
        for table, column in (
            (catalog.unit, catalog.unit.c.edition_id),
            (catalog.volume, catalog.volume.c.edition_id),
            (acquisition.source_binding, acquisition.source_binding.c.edition_id),
        ):
            connection.execute(sa.update(table).where(column == drop_id).values(edition_id=keep_id))
        values: dict[str, Any] = {}
        raised = merged is not None and (ages[0] is None or merged > ages[0])
        if raised:
            age, x = to_columns(merged)
            values.update(age_rating=age, age_x=x)
        if plan.unrated_after:
            values["rated_at"] = None  # never rated as a whole: the barrier of EF-BIB-055
        if values:
            connection.execute(
                sa.update(catalog.edition).where(catalog.edition.c.id == keep_id).values(**values)
            )
        # A suggested age of the donor still blocks: it follows the Units it was about.
        connection.execute(
            sa.update(catalog.age_suggestion)
            .where(
                catalog.age_suggestion.c.entity_type == "edition",
                catalog.age_suggestion.c.entity_id == drop_id,
            )
            .values(entity_id=keep_id)
        )
        connection.execute(sa.delete(catalog.edition).where(catalog.edition.c.id == drop_id))
        savepoint.commit()
    except exc.IntegrityError:
        savepoint.rollback()  # a collision the plan could not see: nothing moved
        raise EditionMergeRefusedError(plan) from None
    filing = file_new_edition(connection, keep_id, keep.shelf_id)  # the gate, again
    if audit is not None:
        audit(connection, plan)
    return EditionMerged(plan, filing)


def plan_after_contributor_merge(
    connection: Connection,
    keep_contributor_id: str,
    drop_contributor_id: str,
    *,
    screen: TombstoneScreen,
) -> tuple[EditionMergePlan, ...]:
    """The plans of the Editions that would share a key once `drop` is merged into `keep` as a
    translator (`text_version`, EF-BIB-049): proposed, never applied. An Edition of the dropped
    contributor that has no twin needs no merge. Nothing is written."""
    e = catalog.edition.c
    dropped = connection.execute(
        sa.select(catalog.edition).where(e.text_version_contributor_id == drop_contributor_id)
    ).all()
    plans: list[EditionMergePlan] = []
    for row in dropped:
        twin = connection.execute(
            sa.select(e.id).where(
                e.work_id == row.work_id,
                e.text_version_contributor_id == keep_contributor_id,
                *(
                    e[c] == row._mapping[c]
                    for c in EDITION_KEY
                    if c not in ("work_id", "text_version")
                ),
            )
        ).scalar()
        if twin is not None:
            plans.append(plan_edition_merge(connection, str(twin), row.id, screen=screen))
    return tuple(sorted(plans, key=lambda p: (p.keep_id, p.drop_id)))


def edition_key_of(connection: Connection, edition_id: str) -> Sequence[tuple[str, str]]:
    row = _edition(connection, edition_id)
    return tuple((c, str(row._mapping[c])) for c in EDITION_KEY)
