"""`intents list|resolve` (EF-INT-014): what an interrupted file operation left open, and how an
admin settles it.

`resolve` acts on the library, so it is a reserved action (`intents.resolve`): a fresh session or
the host CLI, never a key, audited with who, which session and when the password was last typed.
It has two shapes. `replay` (the default) applies the rule of the boot to this one intent: the disk
is looked at and the intent is completed or replayed only if it matches exactly one known step of
the operation; anything else stays open and is reported as drift, nothing touched. `abandon` gives
up an intent without touching the disk, which lifts the protection its paths had from the planner
and the reconciliation: the identifier is typed again as the confirmation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.export.application.intents import OPEN_STATES, IntentJournal, ResolveReport
from shelfhold.export.application.manifest_write import adopt_written_hook
from shelfhold.identity.public import RESERVED_ACTIONS, Allowed, Principal, authorize_reserved
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import coherence, identity, platform
from shelfhold.ops.errors import (
    CodedRefusal,
    InvalidChoiceError,
    NotWaitingError,
    StillWaitingError,
    UnknownSubjectError,
)

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["ACTIONS", "IntentOps"]

ACTIONS = ("replay", "abandon")


def _outcome(intent_id: str, report: ResolveReport) -> str:
    for name, ids in (
        ("completed", report.completed),
        ("replayed", report.replayed),
        ("stale_from_backup", report.stale_from_backup),
        ("cancelled", report.abandoned),
    ):
        if intent_id in ids:
            return name
    return "open"


class IntentOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def _journal(self) -> IntentJournal | None:
        loaded = self._comp.load_roots()
        if loaded is None:  # a fresh install has no journal yet
            return None
        return IntentJournal(
            self._comp.engine, self._comp.open_fsops(loaded), clock=self._comp.clock
        )

    def list_open(self) -> list[dict[str, Any]]:
        table = coherence.fs_intent
        with self._comp.engine.connect() as connection:
            current = connection.execute(sa.select(platform.instance.c.restore_epoch)).scalar()
            rows = connection.execute(
                sa.select(table)
                .where(table.c.state.in_(OPEN_STATES))
                .order_by(table.c.created_at, table.c.id)
            ).all()
        return [
            {
                "id": r.id, "op": r.op, "state": r.state, "src": r.src_path, "dst": r.dst_path,
                "plan_id": r.plan_id, "epoch": r.epoch,
                "created_at": r.created_at.isoformat() if r.created_at else None,
                "stale_from_backup": current is not None and r.epoch < current,
            }
            for r in rows
        ]  # fmt: skip

    def resolve(
        self, intent_id: str, *, action: str, confirm: str | None, principal: Principal
    ) -> dict[str, Any]:
        if action not in ACTIONS:
            raise InvalidChoiceError("choose `replay` or `abandon`")
        now = self._comp.clock.now()
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["intents.resolve"], now=now,
            fresh_auth_s=self._comp.settings.fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):  # the route and the socket decided first
            raise CodedRefusal("forbidden", type(verdict).__name__)
        if action == "abandon" and confirm != intent_id:
            raise InvalidChoiceError("type the identifier of the intent to give it up")
        if intent_id not in {i["id"] for i in self.list_open()}:
            self._refuse_not_open(intent_id)
        if action == "abandon":
            journal = self._journal()
            if journal is None or not journal.abandon(intent_id):
                raise NotWaitingError("this intent is not open")
            outcome, reason = "cancelled", ""
        else:
            outcome, reason = self._replay(intent_id)
        self._audit(principal, intent_id, action)
        if outcome == "open":
            raise StillWaitingError(reason)
        return {"intent_id": intent_id, "outcome": outcome}

    def _refuse_not_open(self, intent_id: str) -> None:
        with self._comp.engine.connect() as connection:
            known = connection.execute(
                sa.select(coherence.fs_intent.c.id).where(coherence.fs_intent.c.id == intent_id)
            ).first()
        if known is None:
            raise UnknownSubjectError("no such intent")
        raise NotWaitingError("this intent is not open")

    def _replay(self, intent_id: str) -> tuple[str, str]:
        with self._comp.write_context() as w:  # the lease: a replay writes
            journal = IntentJournal(self._comp.engine, w.fsops, clock=self._comp.clock)
            hook = adopt_written_hook(w.fsops, w.loaded.locate, self._comp.clock)
            report = journal.resolve_one(intent_id, on_done=hook)
        if report is None:
            return "open", "the intent is not open any more"
        outcome = _outcome(intent_id, report)
        reason = report.drift_reasons.get(intent_id, "an intent of a format this build ignores")
        return outcome, reason if outcome == "open" else ""

    def _audit(self, principal: Principal, intent_id: str, action: str) -> None:
        with self._comp.engine.begin() as connection:
            connection.execute(
                sa.insert(identity.audit).values(
                    id=new_ulid(),
                    actor_id=principal.actor_id,
                    action="intents.resolve",
                    target=f"intent:{intent_id}",
                    session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
