"""The priority of a new source binding (`UNIQUE (edition_id, priority)`).

An Edition may be bound to several sources; each new binding comes after the ones it already has.
One function for every place that writes a binding, so that none of them fixes the priority to 1.
"""

from sqlalchemy import Connection, func, select

from shelfhold.infrastructure.schema import acquisition

__all__ = ["next_binding_priority"]


def next_binding_priority(connection: Connection, edition_id: str) -> int:
    """The next free priority of an Edition's sources: the highest one it has, plus one."""
    table = acquisition.source_binding
    top = connection.execute(
        select(func.max(table.c.priority)).where(table.c.edition_id == edition_id)
    ).scalar()
    return int(top or 0) + 1
