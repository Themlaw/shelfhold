"""Related Works: a typed link between two Works that stay distinct (EF-BIB-039).

A novel and its webtoon adaptation, a sequel, a spin-off: each is a Work of its own, with its
Editions, its Units and its rating. A relation never merges them (a merge is EF-BIB-019, a
decision of its own), never moves an Edition and never touches a Series. Only the link is written;
the Kavita "related series" push and the screen are M4.
"""

from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.exc import IntegrityError

from shelfhold.infrastructure.schema import catalog

__all__ = ["RelatedWork", "RelationError", "relate_works", "relations_of", "unrelate_works"]


class RelationError(Exception):
    """A relation that cannot exist: a Work with itself, an unknown Work or kind."""


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
