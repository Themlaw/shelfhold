"""A secret handed from a request to the job that needs it, in memory and for one run (M4 L34).

The queue's ledger is a table: whatever a job is given there stays on disk, in the backups and in
the diagnostic bundle. The password of the Kavita account that a full detachment needs to revoke
the dedicated key is therefore never given to the queue. The door puts it here, under the key of
the job; the job takes it, once, when it starts; a restart, a timeout or a second take find
nothing, and the job says what it could not do (the key stays; the person gives the order again).
"""

import threading
import time
from collections.abc import Callable

__all__ = ["CredentialHandoff"]

DEFAULT_TTL_S = 3600.0


class CredentialHandoff:
    def __init__(
        self, ttl_s: float = DEFAULT_TTL_S, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._held: dict[str, tuple[str, float]] = {}

    def put(self, key: str, secret: str) -> None:
        with self._lock:
            self._drop_expired()
            self._held[key] = (secret, self._clock() + self._ttl_s)

    def take(self, key: str) -> str | None:
        """The secret of this key, once: it is forgotten as it is given."""
        with self._lock:
            self._drop_expired()
            found = self._held.pop(key, None)
        return None if found is None else found[0]

    def holds(self, key: str) -> bool:
        with self._lock:
            self._drop_expired()
            return key in self._held

    def discard(self, key: str) -> None:
        """Forget the secret of this key: the job it was for will not come to take it."""
        with self._lock:
            self._held.pop(key, None)

    def clear(self) -> None:
        with self._lock:
            self._held.clear()

    def _drop_expired(self) -> None:
        now = self._clock()
        for key in [k for k, (_secret, until) in self._held.items() if until <= now]:
            del self._held[key]

    def __repr__(self) -> str:  # never a value, never a key
        return f"CredentialHandoff(held={len(self._held)})"
