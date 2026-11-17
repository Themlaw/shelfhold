"""The webhook sweep over a real instance (EF-INT-005): the sender is built when the keyring exists
(a fresh install has no secret to open), like the notification sweep."""

from __future__ import annotations

from shelfhold.composition import Composition, KeyringUnavailableError
from shelfhold.integrations.webhooks import Deliverer

__all__ = ["LazyWebhookSweep"]


class LazyWebhookSweep:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def run_once(self) -> object:
        try:
            vault = self._comp.vault()
        except KeyringUnavailableError:
            return None
        comp = self._comp
        return Deliverer(
            comp.engine, vault, comp.clock, comp.network.resolver,
            transport=comp.network.transport,
        ).run_once()  # fmt: skip
