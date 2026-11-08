"""The host side of the WakeHook (EF-INT-022, ADR-0025): when to wake, renew and give up.

`Wakes` wraps an optional `WakeHook`. A tool is woken before a batch and before each check; a
session already open is not asked for again; a long job keeps its session open by renewing it
every half of its duration. A wake that fails is a *deferral*, never a failure of the tool: the
batch is put off, nothing is counted against any source or destination (L15 included), and
`wake.deferred` is announced when three batches in a row were put off (matrix rows `sablier_slow`
and `kavita_asleep_sablier`). The count lives in `app_setting`, one key per tool, so it spans the
processes that run the batches.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import platform
from shelfhold.sdk.connected import WakeHook, WakeTimeout

log = structlog.get_logger(__name__)

DEFERRED_AFTER = 3  # batches in a row put off before the event
RETRY_AFTER = timedelta(seconds=30)  # a hook that failed is not asked again before this
_KEY = "wake.deferred."
_table = platform.app_setting


class WakeDeferred(Exception):
    """The tool is not awake and could not be woken: put the batch off, count nothing."""

    def __init__(self, target: str) -> None:
        super().__init__(f"{target} is asleep: the batch is deferred")
        self.target = target


class Wakes:
    def __init__(
        self,
        hook: WakeHook | None,
        targets: frozenset[str],
        *,
        engine: sa.Engine,
        bus: EventBus,
        clock: Clock,
        session_duration: timedelta,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        deferred_after: int = DEFERRED_AFTER,
    ) -> None:
        self._hook = hook
        self._targets = targets
        self._engine = engine
        self._bus = bus
        self._clock = clock
        self._session = session_duration
        self._sleep = sleep
        self._after = deferred_after
        self._awake_until: dict[str, datetime] = {}
        self._blocked_until: dict[str, datetime] = {}

    def attached(self, target: str) -> bool:
        return self._hook is not None and target in self._targets

    async def ensure(self, target: str) -> None:
        """Return when `target` is awake, or raise `WakeDeferred`."""
        if self._hook is None or target not in self._targets:
            return
        now = self._clock.now()
        if self._awake_until.get(target, now) > now:
            return  # the session of the last wake still holds
        if self._blocked_until.get(target, now) > now:
            raise WakeDeferred(target)  # asked a moment ago and failed: not counted twice
        try:
            await self._hook.wake(target)
        except Exception as error:
            self._blocked_until[target] = now + RETRY_AFTER
            reason = "timeout" if isinstance(error, WakeTimeout) else "unreachable"
            log.warning("wake.deferred", target=target, reason=reason)
            self._defer(target, reason)
            raise WakeDeferred(target) from None
        self._awake_until[target] = now + self._session / 2
        self._blocked_until.pop(target, None)
        self._clear(target)

    @asynccontextmanager
    async def session(self, target: str) -> AsyncIterator[None]:
        """Wake `target`, and keep its session open for as long as the job inside runs."""
        await self.ensure(target)
        if self._hook is None or target not in self._targets:
            yield
            return
        renewing = asyncio.create_task(self._renew(self._hook, target))
        try:
            yield
        finally:
            renewing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await renewing

    async def _renew(self, hook: WakeHook, target: str) -> None:
        while True:
            await self._sleep(self._session.total_seconds() / 2)
            try:
                await hook.renew(target)
                self._awake_until[target] = self._clock.now() + self._session / 2
            except Exception as error:  # the next batch wakes it again: this one goes on
                log.warning("wake.renew_failed", target=target, reason=type(error).__name__)

    # -- the deferred batches, in app_setting -------------------------------------------------
    def _defer(self, target: str, reason: str) -> None:
        key = _KEY + target
        with self._engine.begin() as connection:
            stored = connection.execute(
                sa.select(_table.c.value).where(_table.c.key == key)
            ).scalar()
            count = int(stored or 0) + 1
            connection.execute(
                sqlite_insert(_table)
                .values(key=key, value=str(count))
                .on_conflict_do_update(index_elements=["key"], set_={"value": str(count)})
            )
            published = (
                self._bus.publish(connection, "wake.deferred", {"reason": reason, "batches": count})
                if count == self._after
                else None
            )
        if published is not None:
            self._bus.deliver([published])

    def _clear(self, target: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(sa.delete(_table).where(_table.c.key == _KEY + target))
