"""The audit of the accounts of the Komga servers, from the outside (EF-EXP-047, ENF-SEC-016).

The admin's on-demand twin of the `target.komga_audit` job: the same body, run now. `entries`
is the last answer of each target (what the audit page shows), `run` audits every connected
Komga now — read only toward the servers, one audit at a time — and answers as `entries` does.
Nothing here writes to Komga; a target whose key cannot be opened is skipped by the client
factory of the job, never counted clean. What comes out holds closed words and counts, never
the name of an account.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import Engine

from shelfhold.export.application import komga_audit_state as state
from shelfhold.export.application.target_state import load_health
from shelfhold.infrastructure.clock import Clock
from shelfhold.ops.errors import AuditRunningError

__all__ = ["KomgaAccountsOps", "audit_entry"]


def _fresh(audit: dict[str, Any], now: datetime) -> bool:
    """The 36 h of L08b, read on the clock of the caller: an older proof is no proof."""
    try:
        at = datetime.fromisoformat(str(audit.get("audited_at")))
    except ValueError:
        return False
    return at.tzinfo is not None and now - at <= state.MAX_AGE


def audit_entry(engine: Engine, target_id: str, *, now: datetime) -> dict[str, Any]:
    """The last answer of one target: a state word, the closed code of its reason, the number
    of folders proved and when. Never a key, never the name of an account."""
    with engine.connect() as connection:
        audit = state.load(connection, target_id)
        health = load_health(connection, target_id)
    reason = audit.get("reason")
    proved = audit.get("proved")
    if not audit:
        word = "never_audited"
    elif audit.get("failed") or not _fresh(audit, now):
        word = "unverified"
    elif isinstance(reason, str) and reason:
        word = "alert"
    else:
        word = "ok"
    return {
        "state": word,
        "reason": reason if isinstance(reason, str) and reason else None,
        "proved": len(proved) if isinstance(proved, list) else 0,
        "audited_at": audit.get("audited_at") if isinstance(audit.get("audited_at"), str) else None,
        "version": health.version,
    }  # fmt: skip


class KomgaAccountsOps:
    def __init__(
        self,
        engine: Engine,
        *,
        clock: Clock,
        job: Callable[[], None],
        targets: Callable[[], Iterable[str]],
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._running = threading.Lock()  # one audit at a time: the servers are read, not hammered
        self._job = job
        self._targets = targets

    def entries(self) -> list[dict[str, Any]]:
        """The last answer of each target: what the audit page and the admin route show."""
        now = self._clock.now()
        return [{"target_id": t, **audit_entry(self._engine, t, now=now)} for t in self._targets()]

    def run(self) -> list[dict[str, Any]]:
        """The `target.komga_audit` job, run now (read only toward the servers), then the answer
        `entries` gives. A second audit while one runs is refused: nothing was started."""
        if not self._running.acquire(blocking=False):
            raise AuditRunningError
        try:
            self._job()
        finally:
            self._running.release()
        return self.entries()
