"""The lease a writer of a position takes (M5 L22a, debt of M4 L35).

A job that rewrites files (an upgrade, a team switch, an assembly) holds the lock of the Work for the
whole of the replacement, and looks at the guard "chapter read" under it. A position written during
that time, between the guard and the swap, would mark read a chapter the job then replaces. So every
writer of a position takes the same lock of the same Work before it writes, outside its own
transaction (SQLite has one writer: a lock waited for inside one would stall the job that holds it),
and waits for it; a lock that stays held is `ProgressBusyError`, nothing is written, and the device
or the next pass tries again.

`reading` imports nothing of `export`: the lock is given by the composition root (`hold_work`, the
shape of `export.application.locks.work_lock_for`), and a writer that is given none (a test of a
pure rule) takes none.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import sqlalchemy as sa

from shelfhold.infrastructure.schema import catalog as catalog_schema
from shelfhold.reading.application.progress import _edition_of

__all__ = ["ProgressBusyError", "ProgressLease", "progress_leased", "work_of_carrier"]

# `hold(work_id)`: the lock of that Work, held for the length of the `with`.
ProgressLease = Callable[[str], AbstractContextManager[object]]


class ProgressBusyError(Exception):
    """A job holds the Work: the position is not written now (a 503 with `Retry-After`)."""


def work_of_carrier(connection: sa.Connection, carrier_type: str, carrier_id: str) -> str | None:
    """The Work a file or a kept copy belongs to; `None` when the carrier is unknown."""
    edition = _edition_of(connection, carrier_type, carrier_id)
    if edition is None:
        return None
    found = connection.execute(
        sa.select(catalog_schema.edition.c.work_id).where(catalog_schema.edition.c.id == edition)
    ).scalar()
    return None if found is None else str(found)


@contextmanager
def progress_leased(
    engine: sa.Engine, hold_work: ProgressLease | None, carrier_type: str, carrier_id: str
) -> Iterator[None]:
    """The lease of the Work of the carrier, for the writes inside the `with`. No lease given, or
    a carrier with no Work (the write that follows refuses it as it always did): none taken."""
    if hold_work is None:
        yield
        return
    with engine.connect() as connection:
        work = work_of_carrier(connection, carrier_type, carrier_id)
    if work is None:
        yield
        return
    with hold_work(work):
        yield
