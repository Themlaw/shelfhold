"""The lease of the progress writers, built once for the application (M5 L22a).

`reading` writes the positions and `export` holds the locks of the Works it rewrites; neither
imports the other. This module, above both, gives the first the second's lock, and turns "the lock
is held" into the word `reading` knows: `ProgressBusyError`.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager

import sqlalchemy as sa

from shelfhold.export.application.locks import LockBusyError, work_lock_for
from shelfhold.infrastructure.clock import Clock
from shelfhold.reading.application.lease import ProgressBusyError, ProgressLease

__all__ = ["PROGRESS_LEASE_WAIT_S", "build_progress_lease"]

# How long a writer waits for a replacement of its Work (a render takes seconds, a large volume
# more): past it the device is told to come back, which KOReader and the reader both do.
PROGRESS_LEASE_WAIT_S = 5.0


def build_progress_lease(
    engine: sa.Engine, clock: Clock, *, timeout: float = PROGRESS_LEASE_WAIT_S
) -> ProgressLease:
    hold = work_lock_for(engine, clock=clock, timeout=timeout)

    def lease(work_id: str) -> AbstractContextManager[object]:
        @contextmanager
        def held() -> Iterator[None]:
            with ExitStack() as stack:
                try:
                    stack.enter_context(hold(work_id))
                except LockBusyError:
                    raise ProgressBusyError from None
                yield

        return held()

    return lease
