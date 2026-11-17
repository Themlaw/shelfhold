"""Related Works: a typed link between two Works that stay distinct (EF-BIB-039).

A novel and its webtoon adaptation, a sequel, a spin-off: each is a Work of its own, with its
Editions, its Units and its rating. A relation never merges them (a merge is EF-BIB-019, a
decision of its own), never moves an Edition and never touches a Series. Only the link is written
(`relate_works`, `unrelate_works`: the admin's, M4 L19); what a reader is shown of it is
`relations_shown`, made of the Works she may see and of nothing else; the Kavita "related series"
push is `export.targets.kavita.related`.

A relation that would close a loop (a sequel of its own sequel, however far) is refused: a loop
says nothing true and would make every walk of the chain endless.
"""

from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from shelfhold.catalog.application.queries import (
    ReaderScope,
    first_visible_covers,
    visible_work_ids,
)
from shelfhold.catalog.application.relation_graph import closes_a_loop
from shelfhold.catalog.application.work_merge import resolve_work
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog

__all__ = [
    "MAX_RELATIONS_PER_WORK",
    "RelatedWork",
    "RelationCycleError",
    "RelationError",
    "RelationLimitError",
    "RelationNotFoundError",
    "RelationView",
    "Relations",
    "relate_works",
    "relations_of",
    "relations_shown",
    "unrelate_works",
]

MAX_RELATIONS_PER_WORK = 50  # a Work is related to a handful of others; the walk stays small


class RelationError(Exception):
    """A relation that cannot exist: a Work with itself, an unknown Work or kind."""


class RelationCycleError(RelationError):
    """The relation would close a loop of the same kind (a sequel of its own sequel)."""


class RelationLimitError(RelationError):
    """One of the two Works already holds `MAX_RELATIONS_PER_WORK` relations."""


class RelationNotFoundError(LookupError):
    """A Work of the relation does not exist (an old identifier of a merged Work is not absent: it
    leads to the Work that absorbed it)."""


@dataclass(frozen=True)
class RelationView:
    """One relation as a reader is shown it: the other Work, from the side of this one."""

    work_id: str
    title: str
    kind: str
    outgoing: bool
    edition_id: str | None  # the first Edition she may see, where the other Work's page is
    cover_ref: str | None


@dataclass(frozen=True)
class RelatedWork:
    kind: str  # as the other Work stands to this one: "sequel_of" means it is a sequel of this
    other_work_id: str
    outgoing: bool  # True: this Work is `kind` of the other (from -> to)


def relate_works(
    connection: Connection, from_work_id: str, to_work_id: str, kind: str, *, origin: str,
    now: datetime,
) -> bool:  # fmt: skip
    """`from` is `kind` of `to` (`adaptation_of`, `sequel_of`, `prequel_of`, `spin_off_of`).

    Returns False when the same relation already existed. Both Works remain as they are.
    """
    if from_work_id == to_work_id:
        raise RelationError("a Work is not related to itself")
    table = catalog.work_relation
    known = connection.execute(
        sa.select(table.c.kind).where(
            table.c.from_work_id == from_work_id,
            table.c.to_work_id == to_work_id,
            table.c.kind == kind,
        )
    ).first()
    if known is not None:
        return False
    if any(
        _count(connection, work) >= MAX_RELATIONS_PER_WORK for work in (from_work_id, to_work_id)
    ):
        raise RelationLimitError(f"a Work holds at most {MAX_RELATIONS_PER_WORK} relations")
    if closes_a_loop(connection, from_work_id, to_work_id, kind):
        raise RelationCycleError("this relation would close a loop")
    try:
        with connection.begin_nested():
            connection.execute(
                sa.insert(table).values(
                    from_work_id=from_work_id,
                    to_work_id=to_work_id,
                    kind=kind,
                    origin=origin,
                    created_at=now,
                )
            )
    except IntegrityError:
        raise RelationError("unknown Work or kind of relation") from None
    return True


def _count(connection: Connection, work_id: str) -> int:
    table = catalog.work_relation
    return int(
        connection.execute(
            sa.select(sa.func.count()).where(
                sa.or_(table.c.from_work_id == work_id, table.c.to_work_id == work_id)
            )
        ).scalar_one()
    )


def unrelate_works(connection: Connection, from_work_id: str, to_work_id: str, kind: str) -> bool:
    table = catalog.work_relation
    found = connection.execute(
        sa.delete(table).where(
            table.c.from_work_id == from_work_id,
            table.c.to_work_id == to_work_id,
            table.c.kind == kind,
        )
    )
    return found.rowcount == 1


def relations_of(connection: Connection, work_id: str) -> list[RelatedWork]:
    """What a Work's page shows: every relation, from either end, in a stable order."""
    table = catalog.work_relation
    rows = connection.execute(
        sa.select(table.c.from_work_id, table.c.to_work_id, table.c.kind)
        .where(sa.or_(table.c.from_work_id == work_id, table.c.to_work_id == work_id))
        .order_by(table.c.kind, table.c.from_work_id, table.c.to_work_id)
    )
    return [
        RelatedWork(r.kind, r.to_work_id if r.from_work_id == work_id else r.from_work_id,
                    outgoing=r.from_work_id == work_id)
        for r in rows
    ]  # fmt: skip


def _shown_works(
    connection: Connection, scope: ReaderScope, profile_id: str, work_ids: Sequence[str]
) -> dict[str, tuple[str | None, str | None]]:
    """The Works of `work_ids` this reader may see, each with the first Edition she may see and
    its cover: the one filter already holds the shelves (D-P3). Absent and hidden are the same:
    not in the answer."""
    unique = sorted(set(work_ids))
    seen = sorted(visible_work_ids(connection, scope, unique))
    found = first_visible_covers(connection, scope, seen)
    return {work: found.get(work, (None, None)) for work in seen}


def relations_shown(
    connection: Connection, scope: ReaderScope, profile_id: str, work_id: str
) -> list[RelationView] | None:
    """The relations of a Work as this reader is shown them, or None when the Work is not hers to
    see (absent, hidden and merged into something hidden are the same answer).

    An old identifier leads to the Work that absorbed it. Only the relations whose other end she
    may see are returned: neither the end, nor the kind, nor the count of the others is a fact
    she can read."""
    work_id = resolve_work(connection, work_id)
    if work_id not in _shown_works(connection, scope, profile_id, [work_id]):
        return None
    related = relations_of(connection, work_id)
    shown = _shown_works(connection, scope, profile_id, [r.other_work_id for r in related])
    titles = _titles(connection, [r.other_work_id for r in related if r.other_work_id in shown])
    return [
        RelationView(
            r.other_work_id, titles.get(r.other_work_id, ""), r.kind, r.outgoing,
            shown[r.other_work_id][0], shown[r.other_work_id][1],
        )
        for r in related
        if r.other_work_id in shown
    ]  # fmt: skip


def _titles(connection: Connection, work_ids: Iterable[str]) -> dict[str, str]:
    wanted = sorted(set(work_ids))
    if not wanted:
        return {}
    work = catalog.work.c
    return {
        str(r.id): str(r.title)
        for r in connection.execute(sa.select(work.id, work.title).where(work.id.in_(wanted)))
    }


class Relations:
    """What the HTTP door needs of the relations: the admin's two writes, and the read of a
    reader, in one place so that the door touches no table."""

    def __init__(self, engine: sa.Engine, now: Callable[[], datetime]) -> None:
        self._engine = engine
        self._now = now

    def shown(
        self, scope: ReaderScope, profile_id: str, work_id: str
    ) -> tuple[str, list[RelationView]] | None:
        """`(the Work asked for, as it is now, its relations she is shown)`, or None when the Work
        is not hers to see."""
        with self._engine.connect() as connection:
            resolved = resolve_work(connection, work_id)
            found = relations_shown(connection, scope, profile_id, resolved)
        return None if found is None else (resolved, found)

    def add(self, work_id: str, to_work_id: str, kind: str) -> bool:
        """`work_id` is `kind` of `to_work_id`. False when the relation already existed.
        `RelationNotFoundError`, `RelationError`, `RelationCycleError`; nothing is written then."""
        with write_transaction(self._engine) as connection:
            source, target = resolve_work(connection, work_id), resolve_work(connection, to_work_id)
            if not (_exists(connection, source) and _exists(connection, target)):
                raise RelationNotFoundError(to_work_id)
            return relate_works(connection, source, target, kind, origin="admin", now=self._now())

    def remove(self, work_id: str, to_work_id: str, kind: str) -> bool:
        with write_transaction(self._engine) as connection:
            source, target = resolve_work(connection, work_id), resolve_work(connection, to_work_id)
            return unrelate_works(connection, source, target, kind)


def _exists(connection: Connection, work_id: str) -> bool:
    return (
        connection.execute(sa.select(catalog.work.c.id).where(catalog.work.c.id == work_id)).first()
        is not None
    )
