"""Bench of the wake tests: a recording WakeHook and the coordinator over a real database."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta

import sqlalchemy as sa

from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.wake import Wakes
from shelfhold.sdk.connected import WakeError, WakeTimeout
from tests.fakes_clock import ManualClock


@dataclass
class RecordingHook:
    """A `WakeHook` that records its calls in order, and wakes the simulated tool it fronts."""

    log: list[str] = field(default_factory=list)  # "wake:<target>" / "renew:<target>"
    starts: dict[str, Callable[[], None]] = field(default_factory=dict)
    timeout: bool = False  # the tool never gets ready within the wake delay
    broken: bool = False  # the hook itself fails

    async def wake(self, target: str) -> None:
        self.log.append(f"wake:{target}")
        if self.timeout:
            raise WakeTimeout("not ready within the delay")
        if self.broken:
            raise WakeError("the hook failed")
        self.starts.get(target, lambda: None)()

    async def renew(self, target: str) -> None:
        self.log.append(f"renew:{target}")

    def count(self, kind: str) -> int:
        return sum(1 for line in self.log if line.startswith(kind + ":"))


def make_wakes(
    engine: sa.Engine,
    clock: ManualClock,
    hook: RecordingHook | None,
    targets: frozenset[str],
    *,
    bus: EventBus | None = None,
    session: timedelta = timedelta(minutes=10),
) -> Wakes:
    return Wakes(
        hook, targets, engine=engine, bus=bus or EventBus(clock), clock=clock,
        session_duration=session,
    )  # fmt: skip
