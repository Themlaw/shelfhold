"""What the purges of M1a left in the connected service (EF-BIB-052, EF-SRC-033): a plan, then a
confirmation, never by itself.

M1a withdrew Shelfhold's claim on the downloads of a purged Unit and told the admin to delete in
Suwayomi. The request rows still hold the opaque ticket. When M1b starts, the migration *offers* to
let the service go of those chapters, and to forget the tickets the service no longer knows:

- `plan()` reads only. The service is asked what it holds (`holds`), nothing is erased, and the
  answer is a list of requests (identifiers and nothing else: no title) and a `version`;
- `apply()` is the reserved action `purge.now` (a fresh admin session or the host CLI, never a key)
  with the version the admin read. Each download goes by the deed of "Mettre à l'abri" (an intent
  that names the request, one erasure at most, a deed that was begun is never sent again), so
  running `apply` twice erases nothing twice.

The version covers every request a purge closed, with a ticket or without: a request let go of
leaves it unchanged (the second `apply` has nothing more to do), a new purge changes it.
"""

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Final

import structlog
from sqlalchemy import Engine

from shelfhold.export.application.plans import StalePlanError
from shelfhold.export.application.upstream_port import (
    ALREADY_GONE,
    CANCELLED,
    KEPT_BY_OTHER_ACCOUNT,
    RELEASED,
    UpstreamPort,
)
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    AuditEntry,
    Principal,
    audit_insert,
    authorize_reserved,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.library.public import (
    closed_request_ids,
    leftover_tickets,
    let_go_tickets,
)

__all__ = ["LeftoverItem", "LeftoverPlan", "LeftoverReport", "PurgeLeftovers"]

log = structlog.get_logger(__name__)
ACTION: Final = "purge.now"


@dataclass(frozen=True)
class LeftoverItem:
    request_id: str  # an opaque identifier: never a title, a path or the ticket


@dataclass(frozen=True)
class LeftoverPlan:
    downloads: list[LeftoverItem]  # the service still holds the chapter: it would be let go of
    dead_tickets: list[LeftoverItem]  # the service no longer knows it: the ticket is forgotten
    unknown: int  # the service could not answer for them: left alone, offered again later
    version: str


@dataclass(frozen=True)
class LeftoverReport:
    released: int = 0
    kept_by_other_account: int = 0
    forgotten: int = 0
    unsettled: int = (
        0  # refused, unresolved or unavailable: nothing more is sent, said to the admin
    )


class PurgeLeftovers:
    def __init__(
        self, engine: Engine, upstream: UpstreamPort, *, clock: Clock, fresh_auth_s: int
    ) -> None:
        self._engine = engine
        self._upstream = upstream
        self._clock = clock
        self._fresh_auth_s = fresh_auth_s

    def plan(self) -> LeftoverPlan:
        """Read only. Nothing is erased, and no ticket is changed."""
        with self._engine.connect() as connection:
            tickets = leftover_tickets(connection)
            version = self._version(closed_request_ids(connection))
        held = self._upstream.holds(tickets) if tickets else {}
        return LeftoverPlan(
            downloads=[
                LeftoverItem(t.request_id) for t in tickets if held.get(t.request_id) is True
            ],
            dead_tickets=[
                LeftoverItem(t.request_id) for t in tickets if held.get(t.request_id) is False
            ],
            unknown=sum(1 for t in tickets if t.request_id not in held),
            version=version,
        )

    @staticmethod
    def _version(ids: list[str]) -> str:
        return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()[:32]

    def apply(self, *, if_match: str, principal: Principal, now: datetime) -> LeftoverReport:
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS[ACTION], now=now, fresh_auth_s=self._fresh_auth_s
        )
        if not isinstance(verdict, Allowed):
            raise PermissionError(type(verdict).__name__)  # a key never gets here
        current = self.plan()
        if current.version != if_match:
            raise StalePlanError("the purges changed since the plan was read")
        wanted = {i.request_id for i in current.downloads}
        with self._engine.connect() as connection:
            tickets = [t for t in leftover_tickets(connection) if t.request_id in wanted]
        released = kept = unsettled = 0
        for result in self._upstream.release(tickets) if tickets else []:
            if result.outcome == RELEASED:
                released += 1
            elif result.outcome == KEPT_BY_OTHER_ACCOUNT:
                kept += 1
            elif result.outcome not in (ALREADY_GONE, CANCELLED):
                unsettled += 1
        forgotten = self._forget([i.request_id for i in current.dead_tickets], principal, now)
        log.info(
            "purge.leftovers_applied", released=released, kept=kept, forgotten=forgotten,
            unsettled=unsettled,
        )  # fmt: skip
        return LeftoverReport(released, kept, forgotten, unsettled)

    def _forget(self, request_ids: list[str], principal: Principal, now: datetime) -> int:
        if not request_ids:
            return 0
        with self._engine.begin() as connection:
            count = let_go_tickets(connection, request_ids)
            connection.execute(
                audit_insert(
                    AuditEntry(
                        actor_id=principal.actor_id, action="purge.leftovers_forgotten", at=now,
                        target=f"requests:{count}",
                    )
                )
            )  # fmt: skip
        return int(count)
