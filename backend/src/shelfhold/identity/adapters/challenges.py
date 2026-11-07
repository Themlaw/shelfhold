"""Single-use WebAuthn challenges kept in memory (EF-USR-025).

A challenge lives two minutes, in one process: the instance is unique (lease, ENF-OPS-015) and a
restart only asks the user to start the gesture again, so nothing about a ceremony is written to
disk. Each flow carries 32 random bytes and an unguessable identifier; `consume` removes the
entry whatever the outcome (a wrong purpose or session burns it), so a replay never succeeds.
The table is bounded: an owner (an address, a session) keeps a few flows, the oldest go first.
"""

import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime

from shelfhold.identity.domain.passkeys import CHALLENGE_BYTES, CHALLENGE_TTL, Purpose

PER_OWNER = 8
CAPACITY = 4096


@dataclass(frozen=True)
class Issued:
    flow: str
    challenge: bytes


@dataclass(frozen=True)
class _Entry:
    purpose: Purpose
    bound_to: str
    owner: str
    challenge: bytes
    expires_at: datetime


class MemoryChallenges:
    def __init__(self, *, per_owner: int = PER_OWNER, capacity: int = CAPACITY) -> None:
        self._entries: OrderedDict[str, _Entry] = OrderedDict()
        self._per_owner = per_owner
        self._capacity = capacity
        self._lock = threading.Lock()

    def issue(self, purpose: Purpose, *, bound_to: str, owner: str, now: datetime) -> Issued:
        flow = secrets.token_urlsafe(32)
        entry = _Entry(
            purpose, bound_to, owner, secrets.token_bytes(CHALLENGE_BYTES), now + CHALLENGE_TTL
        )
        with self._lock:
            self._purge(now)
            mine = [key for key, value in self._entries.items() if value.owner == owner]
            for key in mine[: max(0, len(mine) - self._per_owner + 1)]:
                del self._entries[key]
            while len(self._entries) >= self._capacity:
                self._entries.popitem(last=False)
            self._entries[flow] = entry
        return Issued(flow, entry.challenge)

    def consume(self, flow: str, purpose: Purpose, *, bound_to: str, now: datetime) -> bytes | None:
        with self._lock:
            entry = self._entries.pop(flow, None)
        if entry is None or now >= entry.expires_at:
            return None
        if entry.purpose != purpose or not secrets.compare_digest(entry.bound_to, bound_to):
            return None
        return entry.challenge

    def _purge(self, now: datetime) -> None:
        for key in [k for k, v in self._entries.items() if now >= v.expires_at]:
            del self._entries[key]
