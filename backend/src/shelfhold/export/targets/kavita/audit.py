"""The read-only access audit of Kavita (EF-EXP-055, passe 10 K4).

Shelfhold reads the accounts (`GET /api/Users`), never writes. A library is *exposed to
children* when a non-administrator account reaches it with an age restriction or without "include
unknowns". Replayed at every connection and every day: a gap pauses the shelf with
`target.child_exposure`. On a Kavita release classed `known_bad` for `access_control`, **one**
restricted account of the instance pauses **every** shelf linked to it (18+ and X included);
without such an account it is a warning. The way out is `target recheck` after Kavita is updated.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_state import (
    CHILD_EXPOSURE,
    pause_shelf,
    resume_shelf,
    save_health,
)
from shelfhold.export.domain.kavita_access import (
    Account,
    check_link,
    exposed_accounts,
    restricted_accounts,
    tier,
)
from shelfhold.export.targets.kavita import dto
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import export

__all__ = ["AccessAuditor", "AuditReport"]

ACCESS_CONTROL = "access_control"


@dataclass
class AuditReport:
    exposed: dict[str, tuple[str, ...]] = field(default_factory=dict)  # remote library -> names
    paused_shelves: list[str] = field(default_factory=list)  # newly paused by this audit
    resumed_shelves: list[str] = field(default_factory=list)
    known_bad_restricted: tuple[str, ...] = ()  # restricted accounts on an access_control release
    warnings: tuple[str, ...] = ()


def _account(member: dto.Member) -> Account:
    return Account(
        member.username, member.is_admin, member.age_rating, member.include_unknowns,
        member.library_ids,
    )  # fmt: skip


class AccessAuditor:
    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events

    async def accounts(self, client: KavitaClient) -> list[Account]:
        return [_account(m) for m in await client.list_users()]

    async def refused_names(
        self, client: KavitaClient, *, shelf_ceiling: int | None, remote_library_ref: str
    ) -> tuple[str, ...]:
        """Before linking a shelf or adopting a library: who refuses it. Writes nothing."""
        accounts = await self.accounts(client)
        exposed = exposed_accounts(accounts, int(remote_library_ref))
        return check_link(shelf_ceiling, exposed)

    async def audit(
        self, client: KavitaClient, target_id: str, *, resume: bool = False
    ) -> AuditReport:
        """Replayed at each connection and in the daily check; `resume` is `target recheck`."""
        assessment = await client.probe()
        accounts = await self.accounts(client)
        library_ids = frozenset(
            int(lib["id"]) for lib in await client.list_libraries() if "id" in lib
        )
        report = AuditReport(warnings=tuple(client.health.warnings))
        bad = any(k.category == ACCESS_CONTROL for k in assessment.known_bad)
        restricted = restricted_accounts(accounts, library_ids)
        if bad and restricted:
            report.known_bad_restricted = restricted
        pause_all = bad and bool(restricted)
        shelves: list[tuple[str, bool]] = []  # (shelf, must be paused)
        now = self._clock.now()
        with self._engine.begin() as connection:
            for row in self._links(connection, target_id):
                exposed = exposed_accounts(accounts, int(row.remote_ref))
                self._store(connection, row.remote_library_id, exposed, client.health.version, now)
                if exposed:
                    report.exposed[row.remote_ref] = tuple(a.username for a in exposed)
                shelves.append((row.shelf_id, bool(exposed) or pause_all))
            wanted = {shelf for shelf, must in shelves if must}
            for shelf_id in sorted(wanted):
                if pause_shelf(connection, shelf_id, CHILD_EXPOSURE):
                    report.paused_shelves.append(shelf_id)
            if resume:
                for shelf_id in sorted({s for s, _ in shelves} - wanted):
                    if resume_shelf(connection, shelf_id, CHILD_EXPOSURE):
                        report.resumed_shelves.append(shelf_id)
            health = client.health
            health.child_exposure = bool(
                connection.execute(
                    sa.select(sa.func.count())
                    .select_from(export.shelf)
                    .where(export.shelf.c.paused_reason == CHILD_EXPOSURE)
                ).scalar_one()
            )
            health.refresh_waiting()
            save_health(connection, target_id, health)
        if report.paused_shelves:
            accounts_named = sorted({n for names in report.exposed.values() for n in names})
            self._events.emit(
                "target.child_exposure", target_id=target_id, shelves=report.paused_shelves,
                accounts=accounts_named or list(report.known_bad_restricted),
                cause=("access_control_known_bad" if pause_all else "exposed_library"),
            )  # fmt: skip
        return report

    # -- the database --------------------------------------------------------------------------

    @staticmethod
    def _links(connection: sa.Connection, target_id: str) -> list[sa.Row[tuple[str, str, str]]]:
        d, link, lib = (
            export.shelf_destination,
            export.shelf_destination_remote_library,
            export.remote_library,
        )
        return list(
            connection.execute(
                sa.select(d.c.shelf_id, lib.c.id.label("remote_library_id"), lib.c.remote_ref)
                .select_from(
                    d.join(link, link.c.shelf_destination_id == d.c.id).join(
                        lib, lib.c.id == link.c.remote_library_id
                    )
                )
                .where(d.c.target_id == target_id)
                .order_by(d.c.id)
            )
        )

    @staticmethod
    def _store(
        connection: sa.Connection,
        remote_library_id: str,
        exposed: tuple[Account, ...],
        version: str | None,
        now: object,
    ) -> None:
        t = export.kavita_access_audit
        values = {
            "accounts": [
                {
                    "username": a.username, "age_rating": a.age_rating,
                    "include_unknowns": a.include_unknowns,
                }
                for a in exposed
            ],
            "age_restriction": max((tier(a) for a in exposed), default=None),
            "include_unknowns": any(a.include_unknowns for a in exposed) if exposed else None,
            "exposed_to_children": bool(exposed),
            "kavita_version": version,
            "audited_at": now,
        }  # fmt: skip
        done = connection.execute(
            sa.update(t).where(t.c.remote_library_id == remote_library_id).values(**values)
        )
        if done.rowcount == 0:
            connection.execute(sa.insert(t).values(remote_library_id=remote_library_id, **values))
