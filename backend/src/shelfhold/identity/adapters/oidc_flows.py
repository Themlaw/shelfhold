"""OpenID Connect flows kept in memory (M5 L03, EF-USR-004).

A flow lives ten minutes in one process: the instance is unique (lease, ENF-OPS-015) and a restart
only asks the person to start again, so nothing about a sign-in in progress is written to disk.
The table is bounded: an owner (an address, an account) keeps a few flows and the oldest go first;
`consume` removes the entry whatever the outcome, so a replay never succeeds.
"""

from __future__ import annotations

import hashlib
import threading
from collections import OrderedDict
from datetime import datetime

from shelfhold.identity.application.oidc import OidcFlow

PER_OWNER = 8
CAPACITY = 4096


def _key(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()  # the state itself is never the key


class MemoryOidcFlows:
    def __init__(self, *, per_owner: int = PER_OWNER, capacity: int = CAPACITY) -> None:
        self._entries: OrderedDict[str, tuple[str, OidcFlow]] = OrderedDict()
        self._per_owner = per_owner
        self._capacity = capacity
        self._lock = threading.Lock()

    def issue(self, state: str, flow: OidcFlow, *, owner: str, now: datetime) -> None:
        with self._lock:
            live = OrderedDict((k, v) for k, v in self._entries.items() if now < v[1].expires_at)
            mine = [key for key, value in live.items() if value[0] == owner]
            for key in mine[: max(0, len(mine) - self._per_owner + 1)]:
                live.pop(key)
            while len(live) >= self._capacity:
                live.popitem(last=False)
            live[_key(state)] = (owner, flow)
            self._entries = live

    def consume(self, state: str) -> OidcFlow | None:
        with self._lock:
            entry = self._entries.pop(_key(state), None)
        return None if entry is None else entry[1]
