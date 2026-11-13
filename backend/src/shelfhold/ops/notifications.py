"""Per-person notifications over a real instance (EF-INT-021): the router on the bus, the sweep.

The router is subscribed once, where the application is composed, next to the arrival of requests
it listens to. The sweep is a job (`jobs.digest_job`); its sender is built when the keyring
exists (a fresh install has no channel to open), like the operating dispatcher.
"""

from __future__ import annotations

from typing import Any

from shelfhold.composition import Composition, KeyringUnavailableError
from shelfhold.integrations.notify import (
    ChannelDelivery,
    NotificationRouter,
    NotificationSender,
)
from shelfhold.jobs.digest_job import KIND
from shelfhold.ops.status import pending_withdrawal_count

__all__ = ["LazyNotificationSweep", "subscribe_notifications"]


def subscribe_notifications(comp: Composition) -> NotificationRouter:
    """A request arrived or a chapter was filed: the people it concerns are queued. An urgent
    notice wakes the sweep instead of waiting for its next turn."""

    def wake() -> None:
        pools: Any = comp.pools()
        if pools is not None:
            pools.submit(KIND, dedupe_key=KIND)

    router = NotificationRouter(comp.engine, comp.clock, wake=wake)
    router.subscribe(comp.bus)
    return router


class LazyNotificationSweep:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def run_once(self) -> object:
        try:
            vault = self._comp.vault()
        except KeyringUnavailableError:
            return None
        comp = self._comp
        return NotificationSender(
            comp.engine, comp.clock,
            deliver=ChannelDelivery(comp.engine, vault, comp.clock),
            pending_withdrawals=pending_withdrawal_count,
            private_titles=False,  # the instance's channels are read by several people
        ).run_once()  # fmt: skip
