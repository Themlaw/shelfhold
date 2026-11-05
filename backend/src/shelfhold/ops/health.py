"""`/healthz` with the paused queues and their reasons (D11, A7)."""

from __future__ import annotations

from collections.abc import Callable, Mapping

import sqlalchemy as sa

from shelfhold.infrastructure.health import HealthProbe
from shelfhold.ops.ports import PausedQueue
from shelfhold.ops.services import OpsServices

__all__ = ["OpsHealth"]


class OpsHealth:
    """The probe of the instance, plus what waits: the names for `paused_queues`, the reason of
    each for `paused`. Read only; an unreadable database is an empty list (`/readyz` says why)."""

    def __init__(self, probe: HealthProbe, ops: Callable[[], OpsServices]) -> None:
        self._probe = probe
        self._ops = ops

    def migration_progress(self) -> tuple[int, int] | None:
        return self._probe.migration_progress()

    def readiness(self) -> Mapping[str, bool]:
        return self._probe.readiness()

    def _queues(self) -> list[PausedQueue]:
        try:
            return self._ops().paused_queues()
        except sa.exc.SQLAlchemyError:
            return []

    def paused_queues(self) -> list[str]:
        return [queue.queue for queue in self._queues()]

    def paused(self) -> list[dict[str, str]]:
        return [{"queue": q.queue, "reason": q.reason} for q in self._queues()]
