"""The lock of a Work, as the writers of the follow and safeguard jobs take it (EF-BIB-052).

`purge --now` holds `work_resource(work_id)` from its first look to its last write. A job that
files a chapter of that Work (the ingestion of the follow cycle) or copies it to /store ("Mettre à
l'abri") takes the same lock, so that the two never interleave: no file lands, readable, for rows
a purge is erasing or has erased. A Work that stays held past `WAIT_SECONDS` is `WorkBusyError`
and the job tries again at its next cycle.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine

from shelfhold.composition import Composition
from shelfhold.export.application.locks import LockBusyError, ResourceLocks, work_resource
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.library.public import WorkBusyError, WorkHolder

__all__ = ["WAIT_SECONDS", "holder_over", "work_holder"]

WAIT_SECONDS = 10.0


def work_holder(comp: Composition, *, wait: float = WAIT_SECONDS) -> WorkHolder:
    return holder_over(comp.engine, comp.clock, wait=wait)


def holder_over(engine: Engine, clock: Clock, *, wait: float = WAIT_SECONDS) -> WorkHolder:
    locks = ResourceLocks(engine, clock=clock)

    @contextmanager
    def hold(work_id: str) -> Iterator[None]:
        try:
            with locks.hold_resources(new_ulid(), [work_resource(work_id)], timeout=wait):
                yield
        except LockBusyError as busy:
            raise WorkBusyError(work_id) from busy

    return hold
