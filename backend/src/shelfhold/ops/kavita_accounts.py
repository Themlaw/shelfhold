"""The audit of the Kavita accounts of the children, from the outside (EF-USR-017, EF-EXP-036).

It puts the three pieces side by side: the children and the account each declared (identity, read
only), a client of each Kavita target (read only) and the auditor that compares and keeps the
answer. Nothing here writes to Kavita and nothing here names an account in what it returns.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from shelfhold.export.targets.kavita.accounts_audit import (
    ChildAccount,
    ChildAccountsAuditor,
    health_entry,
)
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.identity.public import ChildKavitaAccount
from shelfhold.infrastructure.clock import Clock
from shelfhold.ops.errors import AuditRunningError

__all__ = ["KavitaAccountsOps", "declared_children"]


def declared_children(children: Sequence[ChildKavitaAccount]) -> list[ChildAccount]:
    return [ChildAccount(c.profile_id, c.max_age, c.kavita_username) for c in children]


class KavitaAccountsOps:
    def __init__(
        self,
        engine: Any,
        auditor: ChildAccountsAuditor,
        *,
        clock: Clock,
        targets: Callable[[], Iterable[str]],
        client_for: Callable[[str], KavitaClient],
        children: Callable[[], Sequence[ChildKavitaAccount]],
    ) -> None:
        self._engine = engine
        self._auditor = auditor
        self._clock = clock
        self._running = threading.Lock()  # one audit at a time: Kavita is read, not hammered
        self._targets = targets
        self._client_for = client_for
        self._children = children

    def _declared(self) -> list[ChildAccount]:
        return declared_children(self._children())

    def entries(self) -> list[dict[str, Any]]:
        """The last answer of each target: what the Health page and the admin route show."""
        children = self._declared()
        now = self._clock.now()
        return [
            {"target_id": t, **health_entry(self._engine, t, children=children, now=now)}
            for t in self._targets()
        ]

    def run(self) -> list[dict[str, Any]]:
        """Audit every target now (read only toward Kavita), then answer as `entries` does. A
        target that cannot be read is `unverified`, never clean."""
        if not self._running.acquire(blocking=False):
            raise AuditRunningError
        try:
            children = self._declared()
            for target_id in list(self._targets()):
                try:
                    client = self._client_for(target_id)
                except Exception as error:  # no sealed key, say so: `unverified`, never clean
                    self._auditor.record_failure(target_id, error)
                    continue
                asyncio.run(self._auditor.audit(client, target_id, children))
        finally:
            self._running.release()
        return self.entries()
