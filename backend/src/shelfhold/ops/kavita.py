"""The Kavita side of `target recheck` and `target reconnect` (EF-INT-023)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any

from sqlalchemy import Engine

from shelfhold.export.application.target_state import load_health, save_health
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.reinstall import InstanceService
from shelfhold.infrastructure.clock import Clock
from shelfhold.ops.errors import StillWaitingError

__all__ = ["KavitaTargetOps"]


class KavitaTargetOps:
    """Adapter of the `TargetOps` port over the client, the instance service and the auditor.

    The health document is the state: it is read before, written after, whatever happens (a 403
    during the probe is the very state the admin came to see). `client_for` builds a client with
    its sealed key and its write guard (`kavita.wiring.open_client`).
    """

    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock,
        client_for: Callable[[str], KavitaClient],
        instance: InstanceService,
        auditor: AccessAuditor,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._client_for = client_for
        self._instance = instance
        self._auditor = auditor

    def _client(self, target_id: str) -> KavitaClient:
        client = self._client_for(target_id)
        with self._engine.connect() as connection:
            client.health = load_health(connection, target_id)
        return client

    @staticmethod
    def _run(work: Coroutine[Any, Any, Any]) -> None:
        try:
            asyncio.run(work)
        except TargetError as error:  # the server still refuses: the state stays, with its cause
            raise StillWaitingError(type(error).__name__) from None

    def _save(self, client: KavitaClient, target_id: str) -> None:
        client.health.refresh_waiting()
        with self._engine.begin() as connection:
            save_health(connection, target_id, client.health)

    def recheck(self, target_id: str) -> None:
        """Forget the missing routes and look again; release the shelves whose audit now passes."""
        client = self._client(target_id)
        try:
            self._run(client.recheck())
        finally:
            self._save(client, target_id)
        if client.health.child_exposure:
            self._run(self._auditor.audit(client, target_id, resume=True))

    def reconnect(self, target_id: str) -> None:
        """Take the pauses off: the 403 (rights back) first, then a reinstalled server."""
        client = self._client(target_id)
        try:
            if client.health.paused:
                self._run(client.reconnect())
            if client.health.instance_changed:
                self._run(self._instance.reconnect(client, target_id, auditor=self._auditor))
        finally:
            self._save(client, target_id)
