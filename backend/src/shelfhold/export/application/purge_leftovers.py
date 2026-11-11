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

The series too (EF-BIB-053 (2)): a Work purged in M1a left its series in the service's library, and
only the told "delete in Suwayomi" kept it out. The plan lists the series the service still holds
that a `series_binding` tombstone buries (`UpstreamPort.followed()` read, each series asked of the
tombstones, which are sealed: nothing is decrypted, nothing here names a title: a series is an
opaque digest), and `apply` takes them out of the library (`unfollow`, confirmed by the version).
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
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
    SeriesBindingRef,
    TombstoneGate,
    closed_request_ids,
    leftover_tickets,
    let_go_tickets,
)

__all__ = ["LeftoverItem", "LeftoverPlan", "LeftoverReport", "PurgeLeftovers"]

log = structlog.get_logger(__name__)
ACTION: Final = "purge.now"
PLUGIN_ID: Final = "suwayomi"  # the connected service whose library holds series


def _digest(key: tuple[str, str]) -> str:
    """What the plan says of a series: never its key (a URL slug spells the title)."""
    return hashlib.sha256("|".join(key).encode()).hexdigest()[:16]


@dataclass(frozen=True)
class LeftoverItem:
    request_id: str  # an opaque identifier: never a title, a path or the ticket


@dataclass(frozen=True)
class LeftoverPlan:
    downloads: list[LeftoverItem]  # the service still holds the chapter: it would be let go of
    dead_tickets: list[LeftoverItem]  # the service no longer knows it: the ticket is forgotten
    unknown: int  # the service could not answer for them: left alone, offered again later
    version: str
    series: list[LeftoverItem] = field(default_factory=list)  # buried, still in the library
    series_unknown: bool = False  # the service could not list its library: none are offered


@dataclass(frozen=True)
class LeftoverReport:
    released: int = 0
    kept_by_other_account: int = 0
    forgotten: int = 0
    unsettled: int = 0  # refused, unresolved or unavailable: nothing more is sent
    unfollowed: int = 0  # series taken out of the service's library


class PurgeLeftovers:
    def __init__(
        self,
        engine: Engine,
        upstream: UpstreamPort,
        *,
        clock: Clock,
        fresh_auth_s: int,
        tombstones: TombstoneGate,
        compact: Callable[[], None],
    ) -> None:
        self._tombstones = tombstones
        self._compact = compact
        self._engine = engine
        self._upstream = upstream
        self._clock = clock
        self._fresh_auth_s = fresh_auth_s

    def plan(self) -> LeftoverPlan:
        """Read only. Nothing is erased, and no ticket is changed."""
        with self._engine.connect() as connection:
            tickets = leftover_tickets(connection)
            closed = closed_request_ids(connection)
        held = self._upstream.holds(tickets) if tickets else {}
        series, series_unknown = self._buried_series()
        digests = sorted(_digest(key) for key in series)
        return LeftoverPlan(
            downloads=[
                LeftoverItem(t.request_id) for t in tickets if held.get(t.request_id) is True
            ],
            dead_tickets=[
                LeftoverItem(t.request_id) for t in tickets if held.get(t.request_id) is False
            ],
            unknown=sum(1 for t in tickets if t.request_id not in held),
            version=self._version([*closed, *digests]),
            series=[LeftoverItem(d) for d in digests],
            series_unknown=series_unknown,
        )

    def _buried_series(self) -> tuple[list[tuple[str, str]], bool]:
        """The series the service still lists that a `series_binding` tombstone buries. The
        tombstones are asked, never read: a series that is not buried is none of our business."""
        if self._tombstones is None:
            return [], False
        listed = self._upstream.followed()
        if listed is None:
            return [], True
        buried = [
            key
            for key in sorted(listed)
            if self._tombstones.check(SeriesBindingRef(PLUGIN_ID, *key), root_id=None).blocked
        ]
        return buried, False

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
        # first, and alone refusable: the free pages of the purges of the version before keep their
        # titles (`secure_delete` does not act on the past); a busy database leaves all undone
        self._compact()
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
        unfollowed, failed = self._unfollow(current)
        log.info(
            "purge.leftovers_applied", released=released, kept=kept, forgotten=forgotten,
            unsettled=unsettled,
        )  # fmt: skip
        return LeftoverReport(released, kept, forgotten, unsettled + failed, unfollowed)

    def _unfollow(self, plan: LeftoverPlan) -> tuple[int, int]:
        """The series of the plan, asked again of the service (the plan is stale if it moved), go
        out of its library. Idempotent in the service; a refusal is counted, never retried."""
        wanted = {i.request_id for i in plan.series}
        keys = [key for key in self._buried_series()[0] if _digest(key) in wanted]
        done = failed = 0
        for result in self._upstream.unfollow([(PLUGIN_ID, *key) for key in keys]):
            if result.done:
                done += 1
            else:
                failed += 1
        return done, failed

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
