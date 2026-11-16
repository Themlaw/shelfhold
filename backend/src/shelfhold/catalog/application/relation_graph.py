"""The graph of the order relations between Works (EF-BIB-039): whether one more edge would close
a loop. Shared by the admin's write (`relations`) and by the merge of two Works (`work_merge`), so
that neither can state what the other refuses."""

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.infrastructure.schema import catalog

__all__ = ["closes_a_loop"]

_ORDER_KINDS = ("sequel_of", "prequel_of")  # one graph: `prequel_of` says `sequel_of` the other way


def _edge(from_work_id: str, to_work_id: str, kind: str) -> tuple[str, str, str]:
    """`(graph, follower, followed)`: a sequel points at what it follows; `prequel_of` is the same
    fact written the other way round, so the two share one graph."""
    if kind == "prequel_of":
        return "sequel_of", to_work_id, from_work_id
    return kind, from_work_id, to_work_id


def closes_a_loop(connection: Connection, from_work_id: str, to_work_id: str, kind: str) -> bool:
    """Whether a path leads from `to` back to `from` in the graph of this kind."""
    graph, start, goal = _edge(from_work_id, to_work_id, kind)
    kinds = _ORDER_KINDS if graph == "sequel_of" else (graph,)
    table = catalog.work_relation
    follows: dict[str, set[str]] = {}
    for row in connection.execute(
        sa.select(table.c.from_work_id, table.c.to_work_id, table.c.kind).where(
            table.c.kind.in_(kinds)
        )
    ):
        _graph, a, b = _edge(row.from_work_id, row.to_work_id, row.kind)
        follows.setdefault(a, set()).add(b)
    seen: set[str] = set()
    pending = [goal]  # the walk from `goal` (what `start` would follow) must never reach `start`
    while pending:
        here = pending.pop()
        if here == start:
            return True
        if here not in seen:
            seen.add(here)
            pending.extend(follows.get(here, ()))
    return False
