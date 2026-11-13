"""Source aliases and the bulk migration of a source's series identity (EF-SRC-022, EF-BIB-054 (4)).

A source whose id scheme changes keeps its series: `source_alias` says where a reference moved,
and the plugin's `migrate_ref` can say it too. This module resolves an alias (a chain of aliases is
followed to its end, a cycle is said), plans the migration of every series of a source, and applies
it. Planning writes nothing. Applying rewrites the reference of each `source_item` (and of the
follow requests and pending items that cite it) **in place**: the Works, Editions, Units, Variants
and files are not touched, nothing is queued, nothing is downloaded again.

The tombstones come first. Each series is asked about as a family of references: its own, the end of
its alias chain, every link of the chain, and every alias that leads to one of them (the inverse),
as received and under another spelling. A series with a tombstoned member is **blocked**: never
migrated, whatever the way it got there, and no alias is written for it (an alias cannot bring an
erased series back). With no way to ask the tombstones there is no plan.

Other rows are held, never forced: an alias cycle, a target that is already a series of the library
(no duplicate), or one reference that two series would land on.

The caller holds the write lock for the whole of `plan` + `apply`, so what was checked is what is
written, and one transaction makes it all-or-nothing: an interruption leaves the old state, and the
plan made again after it is the same plan.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.public import EmptyRefError, fold_ref, safe_fold

__all__ = [
    "AliasChain",
    "AliasPlan",
    "ErasedRefs",
    "Family",
    "MigrateRef",
    "Outcome",
    "PlannedRow",
    "Screen",
    "apply_alias_migration",
    "plan_alias_migration",
    "resolve_alias_chain",
]

Ref = tuple[str, str]  # (source_key, remote_id)
Family = tuple[tuple[str, str, str], ...]  # (plugin_id, source_key, remote_id), a member each
Screen = Callable[[Sequence[tuple[str, Family]]], set[str]]  # row id -> blocked by a tombstone
MigrateRef = Callable[[str, str, str], Ref | None]  # (plugin_id, source_key, remote_id) -> new
ErasedRefs = Callable[[str, str], Sequence[str]]  # (plugin_id, source_key) -> erased series refs

MAX_CHAIN = 16
_PLAN_VERSION = 1


class Outcome(StrEnum):
    MIGRATE = "migrate"
    BLOCKED = "blocked"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class AliasChain:
    """`members`: the reference and every link after it; `terminal`: where it ends (None when no
    alias starts from it, or when the chain loops or is too long: `cyclic`)."""

    members: tuple[Ref, ...]
    terminal: Ref | None
    cyclic: bool = False


@dataclass(frozen=True)
class PlannedRow:
    id: str  # the `source_item` row
    plugin_id: str
    old: Ref
    new: Ref | None
    outcome: Outcome
    reason: str
    from_plugin: bool = False  # the new reference came from `migrate_ref`, not from the table


@dataclass(frozen=True)
class AliasPlan:
    source_key: str
    from_scheme: str
    to_scheme: str
    rows: tuple[PlannedRow, ...]
    fingerprint: str
    # (plugin, source key, reference): new forms of **erased** series (what the plugin said for an
    # erased reference, or for a blocked row). The caller seals each as one more tombstone of the
    # same scope, after the commit; nothing readable is ever written (EF-BIB-054 (4)). In memory
    # only: the report, the fingerprint and the record of the migration hold their number.
    new_forms: tuple[tuple[str, str, str], ...] = ()

    def of(self, outcome: Outcome) -> list[PlannedRow]:
        return [r for r in self.rows if r.outcome is outcome]

    @property
    def target(self) -> str:
        return f"{self.from_scheme}>{self.to_scheme}"


def _alias_rows(connection: Connection, plugin_id: str, scheme: str | None) -> dict[Ref, Ref]:
    table = acquisition.source_alias
    query = sa.select(
        table.c.old_source_key, table.c.old_remote_id, table.c.new_source_key,
        table.c.new_remote_id,
    ).where(table.c.plugin_id == plugin_id)  # fmt: skip
    if scheme is not None:  # an alias of no scheme is for every migration
        query = query.where(sa.or_(table.c.id_scheme.is_(None), table.c.id_scheme == scheme))
    return {(r[0], r[1]): (r[2], r[3]) for r in connection.execute(query)}


def _chain(aliases: dict[Ref, Ref], start: Ref) -> AliasChain:
    members = [start]
    seen = {start}
    while members[-1] in aliases:
        following = aliases[members[-1]]
        if following in seen or len(members) >= MAX_CHAIN:
            return AliasChain(tuple(members), None, cyclic=True)
        members.append(following)
        seen.add(following)
    return AliasChain(tuple(members), members[-1] if len(members) > 1 else None)


def resolve_alias_chain(
    connection: Connection,
    plugin_id: str,
    source_key: str,
    remote_id: str,
    *,
    scheme: str | None = None,
    aliases: dict[Ref, Ref] | None = None,
) -> AliasChain:
    """Where a reference ended up: the aliases are followed (bounded) and a loop is said.
    `aliases` is the table already read (a plan resolves every series of a source)."""
    known = aliases if aliases is not None else _alias_rows(connection, plugin_id, scheme)
    return _chain(known, (source_key, remote_id))


def _family(plugin_id: str, refs: set[Ref]) -> Family:
    return tuple(sorted((plugin_id, key, rid) for key, rid in refs))


def _fingerprint(
    source_key: str,
    from_scheme: str,
    to_scheme: str,
    rows: Sequence[PlannedRow],
    new_forms: int = 0,
) -> str:
    """Of the plan, what the admin may see: a blocked row says neither where it would have gone
    nor why, and the new forms of erased series count for a number (a digest of an erased
    reference would let it be guessed)."""
    body = [
        _PLAN_VERSION, source_key, from_scheme, to_scheme,
        sorted([r.id, r.plugin_id, *r.old,
                *((r.new or ("", "")) if r.outcome is Outcome.MIGRATE else ("", "")),
                r.outcome.value, r.reason, r.from_plugin and r.outcome is Outcome.MIGRATE]
               for r in rows),
        new_forms,
    ]  # fmt: skip
    return hashlib.sha256(json.dumps(body, separators=(",", ":")).encode()).hexdigest()


def _folded(ref: Ref) -> Ref:
    return (safe_fold(ref[0]), safe_fold(ref[1]))


def _occupied(connection: Connection, plugin_id: str) -> set[Ref]:
    """The references (folded: two spellings are one series) that are already a series of the
    library, a follow or a pending item."""
    item, follow, pending = acquisition.source_item, acquisition.follow_request, (
        acquisition.pending_source_item
    )  # fmt: skip
    taken = {
        (r[0], r[1])
        for r in connection.execute(
            sa.select(item.c.source_key, item.c.remote_id).where(item.c.plugin_id == plugin_id)
        )
    }
    taken.update(
        (r[0], r[1]) for r in connection.execute(sa.select(follow.c.source_key, follow.c.remote_id))
    )
    taken.update(
        (r[0], r[1])
        for r in connection.execute(sa.select(pending.c.source_key, pending.c.series_ref))
    )
    return {_folded(ref) for ref in taken}


def plan_alias_migration(
    connection: Connection,
    *,
    source_key: str,
    from_scheme: str,
    to_scheme: str,
    migrate_ref: MigrateRef | None,
    screen: Screen,
    erased_refs: ErasedRefs | None = None,
) -> AliasPlan:
    """The migration of every series of `source_key`, and what holds each one back. Reads only."""
    item = acquisition.source_item
    rows: list[PlannedRow] = []
    candidate_forms: list[tuple[str, str, str]] = []
    pending: list[tuple[PlannedRow, set[Ref]]] = []  # candidates, with their family
    plugins = connection.execute(
        sa.select(item.c.plugin_id).where(item.c.source_key == source_key).distinct()
    ).scalars()
    for plugin_id in sorted(plugins):
        aliases = _alias_rows(connection, plugin_id, to_scheme)
        if erased_refs is not None and migrate_ref is not None:
            for erased in erased_refs(plugin_id, source_key):
                said = migrate_ref(plugin_id, source_key, erased)
                if said is None or _folded(said) == _folded((source_key, erased)):
                    continue
                try:
                    fold_ref(said[1])
                except EmptyRefError:
                    continue
                candidate_forms.append((plugin_id, said[0], said[1]))
        found = connection.execute(
            sa.select(item.c.id, item.c.remote_id)
            .where(item.c.plugin_id == plugin_id, item.c.source_key == source_key)
            .order_by(item.c.id)
        ).all()
        for row_id, remote_id in found:
            old: Ref = (source_key, remote_id)
            chain = resolve_alias_chain(
                connection, plugin_id, source_key, remote_id, scheme=to_scheme, aliases=aliases
            )
            if chain.cyclic:
                rows.append(
                    PlannedRow(row_id, plugin_id, old, None, Outcome.UNRESOLVED, "alias_cycle")
                )
                continue
            new, from_plugin = chain.terminal, False
            members = set(chain.members)
            if new is None and migrate_ref is not None:
                new = migrate_ref(plugin_id, source_key, remote_id)
                from_plugin = new is not None
                if new is not None:
                    members.add(new)
            if new is None or new == old:
                continue  # nothing says this series moved
            try:
                fold_ref(new[1])
            except EmptyRefError:  # a target that names nothing is refused
                rows.append(PlannedRow(row_id, plugin_id, old, None, Outcome.UNRESOLVED,
                                       "empty_ref"))  # fmt: skip
                continue
            family = members  # the tombstone check follows every alias, whatever the scheme
            pending.append((PlannedRow(row_id, plugin_id, old, new, Outcome.MIGRATE, "",
                                       from_plugin), family))  # fmt: skip
    blocked = screen([(p.id, _family(p.plugin_id, family)) for p, family in pending])
    survivors: list[tuple[PlannedRow, Ref]] = []
    for planned, _family_of in pending:
        if planned.id in blocked or planned.new is None:  # what a tombstone covers is never moved
            if planned.from_plugin and planned.new is not None:
                candidate_forms.append((planned.plugin_id, *planned.new))
            rows.append(
                PlannedRow(planned.id, planned.plugin_id, planned.old, planned.new, Outcome.BLOCKED,
                           "tombstone", planned.from_plugin)
            )  # fmt: skip
        else:
            survivors.append((planned, planned.new))
    taken = {pl: _occupied(connection, pl) for pl in {s.plugin_id for s, _ in survivors}}
    landing: dict[tuple[str, Ref], int] = {}
    for planned, target in survivors:
        key = (planned.plugin_id, _folded(target))
        landing[key] = landing.get(key, 0) + 1
    for planned, target in survivors:
        folded = _folded(target)
        if folded == _folded(planned.old):
            rows.append(planned)  # only the spelling changes: the row is its own occupant
            continue
        if folded in taken[planned.plugin_id]:
            reason = "target_exists"  # a second series at that reference: never a duplicate
        elif landing[(planned.plugin_id, folded)] > 1:
            reason = "target_shared"  # two series would become one
        else:
            rows.append(planned)
            continue
        rows.append(
            PlannedRow(planned.id, planned.plugin_id, planned.old, None, Outcome.UNRESOLVED, reason)
        )
    rows.sort(key=lambda r: r.id)
    # A form that a tombstone already holds (as received, folded, or through an alias) needs none.
    distinct = sorted(set(candidate_forms))
    held_already = screen([(f"form:{i}", (form,)) for i, form in enumerate(distinct)])
    new_forms = tuple(f for i, f in enumerate(distinct) if f"form:{i}" not in held_already)
    return AliasPlan(
        source_key, from_scheme, to_scheme, tuple(rows),
        _fingerprint(source_key, from_scheme, to_scheme, rows, len(new_forms)), new_forms,
    )  # fmt: skip


def _remember_alias(
    connection: Connection, plugin_id: str, old: Ref, new: Ref, scheme: str
) -> None:
    """One alias per old reference (the table's key): an existing one is kept, never replaced."""
    alias = acquisition.source_alias
    known = connection.execute(
        sa.select(alias.c.id).where(
            alias.c.plugin_id == plugin_id, alias.c.old_source_key == old[0],
            alias.c.old_remote_id == old[1],
        )
    ).first()  # fmt: skip
    if known is None:
        connection.execute(
            sa.insert(alias).values(
                plugin_id=plugin_id, old_source_key=old[0], old_remote_id=old[1],
                new_source_key=new[0], new_remote_id=new[1], id_scheme=scheme,
            )
        )  # fmt: skip


def apply_alias_migration(
    connection: Connection,
    plan: AliasPlan,
    *,
    audit: Callable[[Connection, AliasPlan, str, int, int], None],
) -> int:
    """Move what the plan says, in the caller's transaction (all or nothing). Writes the record of
    the migration and the audit line with it; returns the number of series moved."""
    item, follow, pending = acquisition.source_item, acquisition.follow_request, (
        acquisition.pending_source_item
    )  # fmt: skip
    moved = 0
    for row in plan.of(Outcome.MIGRATE):
        if row.new is None:  # a row that migrates has a target: a plan that says otherwise is wrong
            raise RuntimeError("the plan names no target")
        (old_key, old_id), (new_key, new_id) = row.old, row.new
        done = connection.execute(
            sa.update(item)
            .where(item.c.id == row.id, item.c.plugin_id == row.plugin_id,
                   item.c.source_key == old_key, item.c.remote_id == old_id)
            .values(source_key=new_key, remote_id=new_id)
        )  # fmt: skip
        if done.rowcount != 1:  # the row is not the one that was planned
            raise RuntimeError("the plan no longer matches the library")
        connection.execute(
            sa.update(follow).where(follow.c.source_key == old_key, follow.c.remote_id == old_id)
            .values(source_key=new_key, remote_id=new_id)
        )  # fmt: skip
        connection.execute(
            sa.update(pending).where(pending.c.source_key == old_key,
                                     pending.c.series_ref == old_id)
            .values(source_key=new_key, series_ref=new_id)
        )  # fmt: skip
        moved += 1
    # The alias of what the plugin said is kept for a migrated row: the old reference keeps
    # resolving. An erased or blocked series gets **no** alias (it would write what was erased in
    # clear): its new form is sealed as a tombstone by the caller (`plan.new_forms`).
    for row in plan.of(Outcome.MIGRATE):
        if row.from_plugin and row.new is not None:
            _remember_alias(connection, row.plugin_id, row.old, row.new, plan.to_scheme)
    held = len(plan.rows) - moved
    migration_id = new_ulid()
    counts = {"moved": moved, "held": held, "new_forms": len(plan.new_forms)}
    connection.execute(
        sa.insert(acquisition.source_alias_migration).values(
            id=migration_id, source_key=plan.source_key, from_scheme=plan.from_scheme,
            to_scheme=plan.to_scheme, state="applied", preview=counts,
        )
    )  # fmt: skip
    audit(connection, plan, migration_id, moved, held)
    return moved
