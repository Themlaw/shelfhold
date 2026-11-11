"""The attaches by external identifier on the host CLI (EF-BIB-003, EF-BIB-037, L12).

`edition attachments` lists them (shown), `edition detach` undoes one ("Séparer"),
`edition attach-confirm` shows the removal plan of a suggested attach and applies it for the
token of that plan (the reserved action `rate`), `edition attach-decline` refuses it. Everything
goes through `AutoAttachService`, in one transaction per command.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from shelfhold.catalog.public import (
    AttachPlan,
    AttachPlanStaleError,
    AttachRefusedError,
    AutoAttachService,
)
from shelfhold.identity.public import Principal
from shelfhold.ops.errors import (
    CodedRefusal,
    ExitUnavailableError,
    StalePreviewError,
    UnknownSubjectError,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from shelfhold.catalog.public import EventPublisher
    from shelfhold.infrastructure.clock import Clock
    from shelfhold.ops.tombstone_screens import CatalogScreens

__all__ = ["AttachOps"]


def _plan(plan: AttachPlan) -> dict[str, Any]:
    return {
        "attach_id": plan.attach_id,
        "edition_id": plan.edition_id,
        "work_id": plan.to_work_id,
        "token": plan.token,
        "withdrawals": [
            {"edition_id": w.edition_id, "shelf_id": w.shelf_id, "reason": w.reason.value}
            for w in plan.withdrawals
        ],
    }


class AttachOps:
    def __init__(
        self,
        engine: Engine,
        clock: Clock,
        bus: EventPublisher,
        *,
        fresh_auth_s: int = 900,
        screens: CatalogScreens | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._bus = bus
        self._fresh_auth_s = fresh_auth_s
        self._screens = screens
        self._service = AutoAttachService(events=bus, fresh_auth_s=fresh_auth_s)

    def _screened(self) -> AutoAttachService:
        """A service that asks the Work tombstones (the plan and the confirmation of a suggested
        attach look again: the Work it names may have been purged since). Fails closed."""
        if self._screens is None:
            raise ExitUnavailableError("the Work tombstones cannot be checked here")
        return AutoAttachService(
            events=self._bus, fresh_auth_s=self._fresh_auth_s, tombstones=self._screens.open()
        )

    def attachments(self) -> list[dict[str, str]]:
        with self._engine.connect() as connection:
            return [
                {
                    "attach_id": a.attach_id, "edition_id": a.edition_id,
                    "from_work_id": a.from_work_id, "work_id": a.to_work_id, "state": a.state,
                    "reason": a.reason,
                }
                for a in self._service.attachments(connection)
            ]  # fmt: skip

    def separate(self, attach_id: str, *, principal: Principal) -> dict[str, Any]:
        try:
            with self._engine.begin() as connection:
                done = self._service.separate(
                    connection, attach_id, principal=principal, now=self._clock.now()
                )
        except AttachRefusedError as error:
            raise CodedRefusal("edition.attach_refused", str(error)) from None
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None
        return {
            "attach_id": done.attach_id,
            "edition_id": done.edition_id,
            "work_id": done.work_id,
            "shelf_id": done.filing.shelf_id,
            "to_rate": done.filing.waiting,
            "kept": [f"{k.entity_type}:{k.field}:{k.lang}" for k in done.kept],
            "refile": [{"edition_id": r.edition_id, "shelf_id": r.shelf_id} for r in done.refile],
            "pending_ids": [[e.provider, e.value] for e in done.pending_ids],
        }

    def plan(self, attach_id: str) -> dict[str, Any]:
        service = self._screened()  # the screen is replayed before any write transaction
        try:
            with self._engine.begin() as connection:
                return _plan(service.plan(connection, attach_id))
        except AttachRefusedError as error:
            raise CodedRefusal("edition.attach_refused", str(error)) from None
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None

    def confirm(self, attach_id: str, *, confirm: str, principal: Principal) -> dict[str, Any]:
        service = self._screened()
        try:
            with self._engine.begin() as connection:
                done = service.confirm(
                    connection, attach_id, confirm=confirm, principal=principal,
                    now=self._clock.now(),
                )  # fmt: skip
        except AttachPlanStaleError as error:
            raise StalePreviewError(str(error)) from None
        except AttachRefusedError as error:
            raise CodedRefusal("edition.attach_refused", str(error)) from None
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None
        return _plan(done)

    def decline(self, attach_id: str, *, principal: Principal) -> dict[str, str]:
        try:
            with self._engine.begin() as connection:
                self._service.decline(
                    connection, attach_id, principal=principal, now=self._clock.now()
                )
        except AttachRefusedError as error:
            raise CodedRefusal("edition.attach_refused", str(error)) from None
        except LookupError as error:
            raise UnknownSubjectError(str(error)) from None
        return {"attach_id": attach_id, "state": "declined"}
