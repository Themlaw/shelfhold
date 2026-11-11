"""The lock of a Work, as the writers of the follow and safeguard jobs take it (EF-BIB-052).

`purge --now` holds `work_resource(work_id)` from its first look to its last write. A job that
files a chapter of that Work (the ingestion of the follow cycle) or copies it to /store ("Mettre à
l'abri") takes the same lock, so that the two never interleave: no file lands, readable, for rows
a purge is erasing or has erased. A Work that stays held past `WAIT_SECONDS` is `WorkBusyError`
and the job tries again at its next cycle.
"""

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from typing import TYPE_CHECKING, Any

from sqlalchemy import Engine

from shelfhold.export.application.locks import LockBusyError, ResourceLocks, work_resource
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.library.public import WorkBusyError, WorkHolder

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = [
    "STORE_INDEX_RESOURCE",
    "WAIT_SECONDS",
    "holder_over",
    "index_holder",
    "index_holder_over",
    "work_holder",
]

STORE_INDEX_RESOURCE = "store_index"  # `store/INDEX.jsonl`: the window's pass and `purge --now`

WAIT_SECONDS = 10.0


def work_holder(comp: "Composition", *, wait: float = WAIT_SECONDS) -> WorkHolder:
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


def index_holder(comp: "Composition", *, wait: float = WAIT_SECONDS) -> Callable[[], Any]:
    return index_holder_over(comp.engine, comp.clock, wait=wait)


def index_holder_over(
    engine: Engine, clock: Clock, *, wait: float = WAIT_SECONDS
) -> Callable[[], AbstractContextManager[Any]]:
    """The lock of the index of /store, taken last (after the Work and its folders). A lock still
    held after `wait` seconds is a `LockBusyError`: the caller stops and the next run goes on."""
    locks = ResourceLocks(engine, clock=clock)

    def hold() -> AbstractContextManager[Any]:
        return locks.hold_resources(new_ulid(), [STORE_INDEX_RESOURCE], timeout=wait)

    return hold
