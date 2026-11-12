"""`maintenance on|off` and the control of the queue by API (EF-INT-018, EF-INT-017, EF-INT-014).

The services behind the routes, the socket methods and the CLI. Every change is a reserved action
(`maintenance.enter`, `maintenance.exit`, `queue.control`): a key or a device token is refused, a
role other than admin is refused, and entering or leaving the mode asks for a fresh session. The
state change and its audit line commit in one transaction, so there is no change without a line
and no line for a refusal. This is the last gate: the route and the socket have decided first.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import identity, platform
from shelfhold.jobs.control import (
    PAUSE_CODE,
    USER_CLASSES,
    JobNotFoundError,
    JobStateError,
    PriorityRefusedError,
    QueueControl,
)
from shelfhold.jobs.kinds import traits_of
from shelfhold.jobs.mode import PARK_CODE, MaintenanceMode
from shelfhold.jobs.window import DEFAULT_WINDOW, MaintenanceWindow
from shelfhold.jobs.windows import describe, upcoming
from shelfhold.ops.errors import (
    CodedRefusal,
    InvalidChoiceError,
    NotWaitingError,
    UnknownSubjectError,
)

__all__ = ["MaintenanceOps"]

log = structlog.get_logger(__name__)
ENTER: Final = "maintenance.enter"
EXIT: Final = "maintenance.exit"
QUEUE: Final = "queue.control"
CANCEL_REPAIR: Final = "queue.cancel_repair"
UPCOMING: Final = 3  # the occurrences of the window that `status` lists


def _user(principal: Principal) -> str | None:
    from shelfhold.ops.services import user_of  # the foreign key of the column, as elsewhere

    return user_of(principal)


class MaintenanceOps:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        *,
        fresh_auth_s: int = 900,
        hardened: bool = False,
        window: MaintenanceWindow = DEFAULT_WINDOW,
        control: QueueControl | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._fresh = fresh_auth_s
        self._hardened = hardened
        self._window = window
        self.mode = MaintenanceMode(engine, clock)
        self.control = control if control is not None else QueueControl()

    # -- the gate and the audit -----------------------------------------------------------------

    def _authorize(self, principal: Principal, action: str) -> None:
        verdict = authorize_reserved(
            principal,
            RESERVED_ACTIONS[action],
            now=self._clock.now(),
            fresh_auth_s=self._fresh,
            hardened=self._hardened,
        )
        if not isinstance(verdict, Allowed):  # the route decided first; this is the last gate
            raise CodedRefusal("forbidden", type(verdict).__name__)

    @staticmethod
    def _audit(conn: Connection, principal: Principal, action: str, target: str) -> None:
        conn.execute(
            sa.insert(identity.audit).values(
                id=new_ulid(), actor_id=principal.actor_id, action=action, target=target,
                session_id=principal.session_id, auth_time=principal.auth_time,
            )
        )  # fmt: skip

    # -- the mode -----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        now = self._clock.now()
        with self._engine.connect() as conn:
            state = self.mode.state(conn)
            running = self._count(conn, "running")
            parked = self._count_parked(conn)
            scopes = sorted(
                "all" if kind == "all" else f"source:{key}"
                for kind, key in self.control.paused_scopes(conn)
            )
        return {
            "maintenance": {
                "active": state.active,
                "since": state.since.isoformat() if state.since else None,
                "running_jobs": running,  # still draining when the mode was entered
            },
            "queue": {"paused": scopes, "parked_jobs": parked},
            "window": {
                **asdict(describe(self._window, now)),
                "upcoming": [asdict(v) for v in upcoming(self._window, now, count=UPCOMING)],
            },
        }

    @staticmethod
    def _count_parked(conn: Connection) -> int:
        """What this mode and the queue pause park (the jobs `status` lists under « queue »): not
        the jobs the window or the disk guard parked, which have their own reason."""
        return int(
            conn.execute(
                sa.select(sa.func.count()).where(
                    platform.job_view.c.status == "paused",
                    platform.job_view.c.error_code.in_((PARK_CODE, PAUSE_CODE)),
                )
            ).scalar_one()
        )

    @staticmethod
    def _count(conn: Connection, status: str) -> int:
        return int(
            conn.execute(
                sa.select(sa.func.count()).where(platform.job_view.c.status == status)
            ).scalar_one()
        )

    def enter(self, *, principal: Principal, audit: bool = True) -> dict[str, Any]:
        self._authorize(principal, ENTER)
        with self._engine.begin() as conn:
            try:
                changed = self.mode.enter(conn, by=_user(principal))
            except LookupError:
                raise NotWaitingError("the instance is not initialised") from None
            if changed and audit:
                self._audit(conn, principal, ENTER, "instance")
            running = self._count(conn, "running")
        if changed:
            log.warning("maintenance.entered", running_jobs=running)
        return {"active": True, "changed": changed, "running_jobs": running}

    def leave(self, *, principal: Principal, audit: bool = True) -> dict[str, Any]:
        self._authorize(principal, EXIT)
        with self._engine.begin() as conn:
            changed = self.mode.leave(conn)
            if changed and audit:
                self._audit(conn, principal, EXIT, "instance")
        if changed:
            log.warning("maintenance.exited")
        return {"active": False, "changed": changed}

    # -- the queue ----------------------------------------------------------------------------

    def pause(
        self, source: str | None, *, principal: Principal, audit: bool = True
    ) -> dict[str, Any]:
        self._authorize(principal, QUEUE)
        with self._engine.begin() as conn:
            parked = self.control.pause(
                conn, source=source, at=self._clock.now(), by=_user(principal)
            )
            if audit:
                self._audit(conn, principal, QUEUE, f"pause:{source or 'all'}")
        log.info("queue.paused", scope=source, parked=parked)
        return {"scope": source or "all", "parked": parked}

    def resume(
        self, source: str | None, *, principal: Principal, audit: bool = True
    ) -> dict[str, Any]:
        self._authorize(principal, QUEUE)
        with self._engine.begin() as conn:
            resumed = self.control.resume(conn, source=source)
            if audit:
                self._audit(conn, principal, QUEUE, f"resume:{source or 'all'}")
        log.info("queue.resumed", scope=source, resumed=resumed)
        return {"scope": source or "all", "resumed": resumed}

    def cancel(self, job_id: str, *, principal: Principal, audit: bool = True) -> dict[str, Any]:
        self._authorize(principal, QUEUE)
        with self._engine.begin() as conn:
            kind = conn.execute(
                sa.select(platform.job_view.c.kind).where(platform.job_view.c.id == job_id)
            ).scalar()
            if kind is not None and traits_of(kind).security_repair:
                self._authorize(principal, CANCEL_REPAIR)  # stopping a repair: a fresh session
            try:
                outcome = self.control.cancel(conn, job_id, now=self._clock.now())
            except JobNotFoundError:
                raise UnknownSubjectError("no such job") from None
            except JobStateError as error:
                raise NotWaitingError(str(error)) from None
            if audit:
                self._audit(conn, principal, QUEUE, f"cancel:{job_id}")
        log.info("queue.job_cancelled", job_id=job_id, outcome=outcome.outcome)
        return {"id": job_id, "outcome": outcome.outcome}

    def set_priority(
        self, job_id: str, priority_class: str, *, principal: Principal, audit: bool = True
    ) -> dict[str, Any]:
        self._authorize(principal, QUEUE)
        if priority_class not in USER_CLASSES:
            raise InvalidChoiceError(f"a priority is one of {', '.join(USER_CLASSES)}")
        with self._engine.begin() as conn:
            try:
                chosen = self.control.set_priority(conn, job_id, priority_class)
            except JobNotFoundError:
                raise UnknownSubjectError("no such job") from None
            except JobStateError as error:
                raise NotWaitingError(str(error)) from None
            except PriorityRefusedError as error:
                raise CodedRefusal(error.code, str(error)) from None
            if audit:
                self._audit(conn, principal, QUEUE, f"priority:{job_id}:{chosen}")
        return {"id": job_id, "priority_class": chosen}

    def replay(self, job_id: str, *, principal: Principal, audit: bool = True) -> dict[str, Any]:
        self._authorize(principal, QUEUE)
        with self._engine.begin() as conn:
            try:
                queued, changed = self.control.replay(conn, job_id)
            except JobNotFoundError:
                raise UnknownSubjectError("no such job") from None
            except JobStateError as error:
                raise NotWaitingError(str(error)) from None
            if audit:
                self._audit(conn, principal, QUEUE, f"replay:{job_id}")
        return {"id": queued, "replayed": changed}

    def paused_since(self) -> datetime | None:
        return self.mode.state().since
