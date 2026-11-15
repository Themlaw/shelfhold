"""At most N blocks at a time for the instance, and one for a token (ADR-0053 §8).

A slot is a *lease*. It is taken before the first byte and given back when the stream ends, fails,
is closed or is dropped, and when the transfer is closed by the route as the client goes away
(`interfaces/http_api/opds/routes.py`). None of this can be trusted alone, because a client that
stops reading (a zero TCP window) leaves the serving thread blocked in `send` and nothing in the
stream runs again. So the lease also expires: every chunk handed over proves progress
(`progress`), and a lease with no progress for `send_timeout_s`, or older than `max_duration_s`, is
taken back by the next `acquire`. Its descriptors are closed with it and its stream stops at the
next call. A stalled e-reader cannot hold a slot for ever, and a lost slot is taken back.
"""

import contextlib
import threading
import time
from collections.abc import Callable, Sequence
from typing import Protocol

from shelfhold.reading.bundle.errors import BundleBusyError

RETRY_AFTER_S = 30


class _Closable(Protocol):
    def close(self) -> None: ...


class Lease:
    """One slot. Calling it gives it back (a lease is also the `release` of a slot)."""

    def __init__(self, gate: "Gate", token_id: str | None, now: float) -> None:
        self._gate = gate
        self.token_id = token_id
        self.started = self.touched = now
        self.lost = False  # taken back by the gate: the stream must stop
        self._holds: Sequence[_Closable] = ()

    def hold(self, sources: Sequence[_Closable]) -> None:
        """The descriptors that go with the slot, closed if the slot is taken back."""
        self._holds = tuple(sources)

    def progress(self) -> None:
        self.touched = self._gate.now()

    def release(self) -> None:
        self._gate.drop(self)

    __call__ = release

    def reclaim(self) -> None:
        self.lost = True
        holds, self._holds = self._holds, ()
        for source in holds:
            with contextlib.suppress(Exception):  # a descriptor already closed is closed
                source.close()


class Gate:
    def __init__(
        self,
        *,
        per_token: int,
        per_instance: int,
        send_timeout_s: float = 120.0,
        max_duration_s: float = 8 * 3600.0,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._per_token, self._per_instance = per_token, per_instance
        self._send_timeout, self._max_duration = send_timeout_s, max_duration_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._leases: set[Lease] = set()

    def now(self) -> float:
        return self._monotonic()

    def acquire(self, token_id: str | None) -> Lease:
        now = self._monotonic()
        expired: list[Lease] = []
        lease: Lease | None = None
        with self._lock:
            for old in list(self._leases):
                if now - old.touched > self._send_timeout or now - old.started > self._max_duration:
                    self._leases.discard(old)
                    expired.append(old)
            mine = sum(1 for held in self._leases if held.token_id == token_id)
            full = len(self._leases) >= self._per_instance or (
                token_id is not None and mine >= self._per_token
            )
            if not full:
                lease = Lease(self, token_id, now)
                self._leases.add(lease)
        for old in expired:  # outside the lock: a stalled holder is told, its descriptors freed
            old.reclaim()
        if lease is None:
            raise BundleBusyError(RETRY_AFTER_S)
        return lease

    def drop(self, lease: Lease) -> None:
        with self._lock:
            self._leases.discard(lease)

    def active(self) -> int:
        with self._lock:
            return len(self._leases)
