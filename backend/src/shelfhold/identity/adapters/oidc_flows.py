"""OpenID Connect flows kept in memory (M5 L03, EF-USR-004).

A flow lives ten minutes in one process: the instance is unique (lease, ENF-OPS-015) and a restart
only asks the person to start again, so nothing about a sign-in in progress is written to disk.
The table is bounded three ways: an owner (a /64, an address, an account) keeps a few flows, the
oldest of its own going first; a prefix (a /48, which holds 65 536 /64s) keeps more, whoever of it
asks; the table as a whole has a ceiling. A full prefix or table refuses the new flow rather than
push out another's. `consume` removes the entry whatever the outcome, so a replay never succeeds.
A link the provider has vouched for waits here too, one per browser, for the confirmation.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from datetime import datetime

from shelfhold.identity.application.oidc import OidcFlow, OidcUnavailableError, PendingLink

PER_OWNER = 4  # a /64 or an address (the limiter's own unit), or one account
PER_GROUP = 64  # a /48 whoever of its /64s asks, or one address
CAPACITY = 4096
PENDING_CAPACITY = 1024


def _key(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()  # the state itself is never the key


class MemoryOidcFlows:
    def __init__(
        self,
        *,
        per_owner: int = PER_OWNER,
        per_group: int = PER_GROUP,
        capacity: int = CAPACITY,
        pending_capacity: int = PENDING_CAPACITY,
    ) -> None:
        self._entries: OrderedDict[str, tuple[str, str, OidcFlow]] = OrderedDict()
        self._pending: dict[str, PendingLink] = {}
        self._per_owner = per_owner
        self._per_group = per_group
        self._capacity = capacity
        self._pending_capacity = pending_capacity
        self._lock = threading.Lock()

    def issue(self, state: str, flow: OidcFlow, *, owner: str, group: str, now: datetime) -> None:
        with self._lock:
            live = OrderedDict((k, v) for k, v in self._entries.items() if now < v[2].expires_at)
            mine = [key for key, value in live.items() if value[0] == owner]
            for key in mine[: max(0, len(mine) - self._per_owner + 1)]:
                live.pop(key)  # an owner's own oldest goes first
            self._entries = live
            # never push out another person's flow in progress: the new one is refused when its
            # prefix, or the whole table, has no room left
            if sum(1 for value in live.values() if value[1] == group) >= self._per_group:
                raise OidcUnavailableError("flows_full")
            if len(live) >= self._capacity:
                raise OidcUnavailableError("flows_full")
            live[_key(state)] = (owner, group, flow)

    def consume(self, state: str) -> OidcFlow | None:
        with self._lock:
            entry = self._entries.pop(_key(state), None)
        return None if entry is None else entry[2]

    def hold(self, binder_digest: str, pending: PendingLink, *, now: datetime) -> None:
        with self._lock:
            self._pending = {k: v for k, v in self._pending.items() if now < v.expires_at}
            if binder_digest not in self._pending and len(self._pending) >= self._pending_capacity:
                raise OidcUnavailableError("pending_full")
            self._pending[binder_digest] = pending

    def peek(self, binder_digest: str, *, now: datetime) -> PendingLink | None:
        with self._lock:
            pending = self._pending.get(binder_digest)
        return pending if pending is not None and now < pending.expires_at else None

    def take(self, binder_digest: str, *, now: datetime) -> PendingLink | None:
        with self._lock:
            pending = self._pending.pop(binder_digest, None)
        return pending if pending is not None and now < pending.expires_at else None
