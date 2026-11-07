"""The `restored_behind` exit over the restore guard of L35 (EF-INT-026).

`waiting` is the guard's read-only assessment of the mounted roots against their sentinels. The
adoption itself is not run from here: it needs the instance lock (the server stopped) and a
preview confirmed by its fingerprint, which only `shelfhold reconcile --adopt-after-restore`
offers. A call through the server therefore says so instead of acting.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import Engine

from shelfhold.events.bus import EventBus
from shelfhold.export.application.restore import RestoreGuard
from shelfhold.identity.public import Principal
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fsops import Fsops
from shelfhold.ops.errors import ExitUnavailableError
from shelfhold.ops.ports import WaitingItem

__all__ = ["RestoreGuardExit"]


class RestoreGuardExit:
    """`fsops` is read at each look: the roots of the library change while the server runs
    (`target add`), and a guard opened at start-up would not see the new one."""

    def __init__(
        self, engine: Engine, fsops: Callable[[], Fsops | None], clock: Clock, bus: EventBus
    ) -> None:
        self._engine = engine
        self._fsops = fsops
        self._clock = clock
        self._bus = bus

    def waiting(self) -> list[WaitingItem]:
        fsops = self._fsops()
        if fsops is None:
            return []
        status = RestoreGuard(self._engine, fsops, clock=self._clock, bus=self._bus).assess()
        if not status.behind:
            return []
        return [WaitingItem("restored_behind", "instance", "instance", "database older than disk")]

    def adopt_after_restore(self, *, confirm_deletion: bool, principal: Principal) -> None:
        raise ExitUnavailableError(
            "adoption needs the instance lock and a confirmed preview: stop Shelfhold and run "
            "`shelfhold reconcile --adopt-after-restore`"
        )
