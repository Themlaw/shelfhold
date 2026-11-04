"""Write transactions that take the lock first (SQLite `BEGIN IMMEDIATE`).

An ingest reads, then writes. Started deferred, two of them can both read "no such Variant" and
one fails on the unique key. Started immediate, the second waits for the first and then reads
its result. A residual `IntegrityError` or `SQLITE_BUSY` (the wait outlasted `busy_timeout`) is
replayed once, from scratch, in a new transaction.
"""

from collections.abc import Callable

import sqlalchemy as sa
from sqlalchemy import exc

ATTEMPTS = 2


def _retryable(error: exc.DBAPIError) -> bool:
    if isinstance(error, exc.IntegrityError):
        return True
    text = str(error.orig).lower()
    return isinstance(error, exc.OperationalError) and ("locked" in text or "busy" in text)


def run_immediate[T](engine: sa.Engine, work: Callable[[sa.Connection], T]) -> T:
    for attempt in range(1, ATTEMPTS + 1):
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    result = work(connection)
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                return result
        except exc.DBAPIError as error:
            if attempt == ATTEMPTS or not _retryable(error):
                raise
    raise AssertionError("unreachable")  # pragma: no cover
