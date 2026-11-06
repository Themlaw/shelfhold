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

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.export.application.intents import (
    OPEN_STATES,
    IntentJournal,
    OpenIntent,
    ResolveReport,
)
from shelfhold.export.application.locks import (
    LockBusyError,
    ResourceLocks,
    path_resource,
    work_resource,
)
from shelfhold.export.application.manifest_write import (
    adopt_written_hook,
    release_reserved_hook,
    reserved_line,
)
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
        found = [i for i in self._open_intents() if i.id == intent_id]
        if not found:
            self._refuse_not_open(intent_id)
        with self._held(found[0]):  # a job writing there decides first: nothing is changed
            if action == "abandon":
                outcome, reason = self._abandon(intent_id, principal)
            else:
                outcome, reason = self._replay(intent_id, principal)
        if outcome == "open":
            raise StillWaitingError(reason)
        return {"intent_id": intent_id, "outcome": outcome}

    def _open_intents(self) -> list[OpenIntent]:
        journal = self._journal()
        return [] if journal is None else journal.open_intents()

    @contextmanager
    def _held(self, intent: OpenIntent) -> Iterator[None]:
        """The locks the pipeline holds around this write: the Work and the series folder (or the
        path itself when no line was reserved). Not waited for: an active job holds them, so the
        intent is not an orphan and is not ours to settle now (409, exit code 3)."""
        with self._comp.engine.connect() as connection:
            line = reserved_line(connection, intent)
        if line is not None:
            resources = [
                work_resource(line.work_id),
                path_resource(f"{line.destination_id}/{line.rel_path.split('/', 1)[0]}"),
            ]
        else:
            resources = [path_resource(intent.dst or intent.src or intent.id)]
        locks = ResourceLocks(self._comp.engine, clock=self._comp.clock)
        try:
            with locks.hold_resources(new_ulid(), resources):
                yield
        except LockBusyError:
            raise StillWaitingError("a job is writing there: try again when it is over") from None

    def _refuse_not_open(self, intent_id: str) -> None:
        with self._comp.engine.connect() as connection:
            known = connection.execute(
                sa.select(coherence.fs_intent.c.id).where(coherence.fs_intent.c.id == intent_id)
            ).first()
        if known is None:
            raise UnknownSubjectError("no such intent")
        raise NotWaitingError("this intent is not open")

    def _abandon(self, intent_id: str, principal: Principal) -> tuple[str, str]:
        loaded = self._comp.load_roots()
        if loaded is None:
            raise NotWaitingError("this intent is not open")
        fsops = self._comp.open_fsops(loaded)
        journal = IntentJournal(
            self._comp.engine, fsops, clock=self._comp.clock,
            settled=self._audit(principal),
        )  # fmt: skip
        release = release_reserved_hook(fsops, loaded.locate, self._comp.clock)
        if not journal.abandon(intent_id, release=release):
            raise NotWaitingError("this intent is not open")
        return "cancelled", ""

    def _replay(self, intent_id: str, principal: Principal) -> tuple[str, str]:
        with self._comp.write_context() as w:  # the lease: a replay writes
            journal = IntentJournal(
                self._comp.engine, w.fsops, clock=self._comp.clock,
                settled=self._audit(principal),
            )  # fmt: skip
            hook = adopt_written_hook(w.fsops, w.loaded.locate, self._comp.clock)
            report = journal.resolve_one(intent_id, on_done=hook)
        if report is None:
            return "open", "the intent is not open any more"
        outcome = _outcome(intent_id, report)
        reason = report.drift_reasons.get(intent_id, "an intent of a format this build ignores")
        return outcome, reason if outcome == "open" else ""

    @staticmethod
    def _audit(principal: Principal) -> Callable[[Connection, str], None]:
        """Written in the transaction that closes the intent: no closed intent without its row,
        no row for an intent that stayed open."""

        def write(connection: Connection, intent_id: str) -> None:
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

        return write
