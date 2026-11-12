"""Suggestions of duplicate Works, never grounded on the title alone (EF-BIB-004).

Two Works are suggested to the admin only when the title (case, accents and spacing set aside) is
the same **and** a contributor is credited on both, **and** nothing says they are two: not the
same kind, a place each in one Series, two identifiers of one provider. A translation has another
title, a volume N and N+1 either; a Work whose identifier a `work` tombstone covers is never
proposed. A suggestion is a row of `merge_suggestion` and nothing else: no Work, Edition or
credit is touched, and a pair already decided (rejected or accepted) is not asked again.

The caller owns the transaction.
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.catalog.application.tombstone_screen import TombstoneScreen
from shelfhold.catalog.domain.external_ids import ExternalId
from shelfhold.catalog.domain.identity import sort_title
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog

__all__ = [
    "REASON",
    "SAME_WORK_REASON",
    "Suggestion",
    "open_offset_suggestion",
    "open_suggestions",
    "suggest_duplicates",
    "tombstoned",
]

REASON = "same_title_same_author"
SAME_WORK_REASON = "suwayomi_migration"  # "Même œuvre ?" (EF-BIB-029)


@dataclass(frozen=True)
class Suggestion:
    id: str
    a: str
    b: str
    reason: str
    state: str


def tombstoned(
    connection: Connection, screen: TombstoneScreen, work_ids: Sequence[str]
) -> set[str]:
    t = catalog.external_id
    rows = connection.execute(
        sa.select(t.c.entity_id, t.c.provider, t.c.value).where(
            t.c.entity_type == "work", t.c.entity_id.in_(work_ids)
        )
    ).all()
    hit = screen.work_ids([ExternalId("work", r.provider, r.value) for r in rows])
    return {str(r.entity_id) for r in rows if (r.provider, r.value) in hit}


def suggest_duplicates(connection: Connection, *, screen: TombstoneScreen) -> list[str]:
    """Open a suggestion for each new pair of Works that share a title and a contributor; returns
    the ids of the suggestions made now. `screen` is the Work tombstones: never optional."""
    w, c, m = catalog.work, catalog.contribution, catalog.merge_suggestion
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for work in connection.execute(sa.select(w.c.id, w.c.kind, w.c.title).order_by(w.c.id)):
        groups[(work.kind, sort_title(work.title))].append(str(work.id))
    created: list[str] = []
    for ids in (g for g in groups.values() if len(g) > 1):
        credited: dict[str, set[str]] = defaultdict(set)
        for row in connection.execute(
            sa.select(c.c.entity_id, c.c.contributor_id).where(
                c.c.entity_type == "work", c.c.entity_id.in_(ids)
            )
        ):
            credited[str(row.entity_id)].add(str(row.contributor_id))
        barred = tombstoned(connection, screen, ids)
        for first, second in combinations(ids, 2):
            if first in barred or second in barred:
                continue
            if not credited[first] & credited[second]:
                continue  # the title alone is not a ground
            if told_apart(connection, first, second):
                continue
            suggestion_id = new_ulid()
            done = connection.execute(
                sqlite_insert(m)
                .values(id=suggestion_id, entity_type="work", a=first, b=second, reason=REASON)
                .on_conflict_do_nothing()
            )
            if done.rowcount:
                created.append(suggestion_id)
    return created


def told_apart(connection: Connection, first: str, second: str) -> bool:
    """Two places of one Series, or two identifiers of one provider: two Works, not one."""
    s, e = catalog.series_member, catalog.external_id
    places = {
        str(r.work_id): (r.series_id, r.position)
        for r in connection.execute(
            sa.select(s.c.work_id, s.c.series_id, s.c.position).where(
                s.c.work_id.in_([first, second])
            )
        )
    }
    if len(places) == 2 and places[first] != places[second]:
        series = {places[first][0], places[second][0]}
        if len(series) == 1:
            return True
    held: dict[str, dict[str, str]] = {first: {}, second: {}}
    for r in connection.execute(
        sa.select(e.c.entity_id, e.c.provider, e.c.value).where(
            e.c.entity_type == "work", e.c.entity_id.in_([first, second]), e.c.state == "current"
        )
    ):
        held[str(r.entity_id)][str(r.provider)] = str(r.value)
    return any(
        provider in held[second] and held[second][provider] != value
        for provider, value in held[first].items()
    )


def open_suggestions(connection: Connection) -> list[Suggestion]:
    m = catalog.merge_suggestion
    rows = connection.execute(
        sa.select(m.c.id, m.c.a, m.c.b, m.c.reason, m.c.state)
        .where(m.c.entity_type == "work", m.c.state == "open")
        .order_by(m.c.created_at, m.c.id)
    )
    return [Suggestion(r.id, r.a, r.b, r.reason, r.state) for r in rows]


def open_offset_suggestion(
    connection: Connection, held_variant: str, offered_variant: str, reason: str
) -> None:
    """A fallback the numbering check suspended (EF-BIB-028): the two Variants are put to the admin
    with the reason. One row per pair and reason; nothing else is touched, and nothing is chosen."""
    first, second = sorted((held_variant, offered_variant))
    connection.execute(
        sqlite_insert(catalog.merge_suggestion)
        .values(id=new_ulid(), entity_type="variant", a=first, b=second, reason=reason)
        .on_conflict_do_nothing()
    )
