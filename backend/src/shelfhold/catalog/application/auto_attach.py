"""Attaching an Edition by external identifier: shown, undoable, never past the parental barrier
(EF-BIB-003, EF-BIB-037, EF-BIB-055).

The ingest of a source makes a Work and an Edition for what it brought; `arrive` then looks at the
identifiers it came with. One that another Work already holds (the same AniList id) means it is
the same Work: the Edition is attached to it, and that is

- **gated first**: the gate of the new Edition files it (or not) before anything else, and again
  once attached; new to the Work, it counts as unrated there (`rated_at` cleared), so a shelf
  below 18+ never takes it until an admin rates it;
- **with its age**: the age it had where it came from (its own, else its Work's) and the open
  suggestions of that Work go with it, as suggestions of the Edition (`attach` for the age): rating
  the Work it joins does not rule on them (#126), only `rate_edition` does;
- **shown**: an event (`edition.auto_attached`) and the journal `edition_attach`, listed by
  `edition attachments`;
- **undoable**: "Séparer" (`separate`, CLI `edition detach`) puts back exactly what the attach
  changed: the Work, the rating stamp and the shelf of the Edition (the shelf through the gate
  again: the way back is a road too), the values, provenances and locks of the fields the
  attach wrote, unless an admin changed them since. An attach writes **no** external
  identifier: the ones the Edition brought for its Work stay in the journal, so the undo leaves
  nothing behind in `external_id`, which is
  append-only.

An attach of an Edition whose age of origin (its own, else its Work's) exceeds the effective age of
the Work it would join, or of a Work nobody rated, is never automatic: it is a suggestion
(`edition.attach_suggested`). `plan` shows the removal plan of the shelves below 18+ it would
cause, computed in a savepoint that is rolled back; `confirm`, a reserved action (`rate`), applies
the attach for the token of that plan only and returns the plan,
which `shelf withdraw` applies. Nothing is withdrawn here.

An identifier two Works hold, a holder that no longer exists, or an Edition of the same key in the
holder (a merge of Editions, L14) attach nothing: they are reported. The caller owns the
transaction.
"""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection

from shelfhold.catalog.application.external_ids import (
    ExternalIdConflictError,
    add_external_id,
    holder_of,
)
from shelfhold.catalog.application.fields import (
    keys_of,
    propose_fields,
    read_field,
    restore_field,
)
from shelfhold.catalog.application.new_edition_gate import Filing, file_new_edition
from shelfhold.catalog.application.rating import RatingService, Withdrawal
from shelfhold.catalog.application.tombstone_screen import (
    TombstoneScreen,
    TombstoneScreenMissingError,
)
from shelfhold.catalog.domain.age import Admission
from shelfhold.catalog.domain.external_ids import (
    ExternalId,
    ExternalIdError,
    normalize_external_id,
)
from shelfhold.catalog.domain.provenance import FieldKey, StoredField
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    AuditEntry,
    Credential,
    Principal,
    audit_insert,
    authorize_reserved,
)
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog
from shelfhold.sdk.age import AgeRating
from shelfhold.sdk.metadata import FieldProposal

__all__ = [
    "ATTACH_SUGGESTED",
    "AUTO_ATTACHED",
    "Arrival",
    "AttachPlan",
    "AttachPlanStaleError",
    "AttachRefusedError",
    "AttachView",
    "AutoAttachService",
    "Detached",
    "EventPublisher",
    "IncomingEdition",
    "Refiling",
]

AUTO_ATTACHED = "edition.auto_attached"
ATTACH_SUGGESTED = "edition.attach_suggested"
_ID_ORIGINS = {"provider": "provider", "manual": "manual"}  # anything else came with a source
_KEY = (
    "lang",
    "translation_type",
    "media",
    "publisher",
    "text_version",
    "performance",
    "packaging",
)


class EventPublisher(Protocol):
    """The event bus as this service uses it (`events.bus.EventBus` is one)."""

    def publish(
        self,
        connection: Connection,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        *,
        actor_id: str | None = None,
        visibility: str = "ops",
    ) -> object: ...


class AttachRefusedError(Exception):
    """Not this caller, or not in this state (already undone, moved since, not a suggestion)."""


class AttachPlanStaleError(Exception):
    """The plan moved since it was read: read it again (`plan.stale`)."""

    code = "plan.stale"


@dataclass(frozen=True)
class IncomingEdition:
    """What the ingest of a source made, and what it came with."""

    edition_id: str  # in the Work the ingest made for it
    shelf_id: str | None = None  # the shelf the source proposes
    ids: tuple[ExternalId, ...] = ()  # of its Work and of itself
    fields: tuple[FieldProposal, ...] = ()
    origin: str = "source"  # the provenance of its fields: source, provider, embedded, import
    keep_filed: bool = False  # it joined an Edition already on a shelf: never moved (no withdrawal)


@dataclass(frozen=True)
class Arrival:
    edition_id: str
    work_id: str  # where the Edition is now
    state: str  # kept | attached | suggested | conflict
    filing: Filing
    attach_id: str | None = None
    conflicts: tuple[str, ...] = ()  # codes: ambiguous, holder_missing, edition_key, ...


@dataclass(frozen=True)
class AttachView:
    attach_id: str
    edition_id: str
    from_work_id: str
    to_work_id: str
    state: str
    reason: str
    created_at: datetime


@dataclass(frozen=True)
class AttachPlan:
    attach_id: str
    edition_id: str
    to_work_id: str
    withdrawals: tuple[Withdrawal, ...]  # what the attach makes leave a shelf below 18+
    token: str  # what `confirm` asks for


@dataclass(frozen=True)
class Detached:
    attach_id: str
    edition_id: str
    work_id: str  # where it is back
    filing: Filing
    kept: tuple[FieldKey, ...]  # fields an admin changed since the attach: left as they are
    refile: tuple["Refiling", ...] = ()  # sisters the confirmed attach withdrew, admitted again
    pending_ids: tuple[ExternalId, ...] = ()  # what the Edition brought for its Work: to record


@dataclass(frozen=True)
class Refiling:
    """A shelf an Edition may go back to: proposed, never applied here (the admin files it)."""

    edition_id: str
    shelf_id: str


def _stored_json(stored: StoredField | None) -> dict[str, Any] | None:
    if stored is None:
        return None
    return {
        "value": stored.value,
        "provider": stored.provider,
        "provenance": stored.provenance,
        "confidence": stored.confidence,
        "locked": stored.locked,
        "fetched_at": stored.fetched_at.isoformat(),
    }


def _stored(data: Mapping[str, Any] | None) -> StoredField | None:
    if data is None:
        return None
    return StoredField(
        data["value"],
        data["provider"],
        data["provenance"],
        data["confidence"],
        bool(data["locked"]),
        datetime.fromisoformat(data["fetched_at"]),
    )


def _age(age_rating: int | None, age_x: bool) -> AgeRating | None:
    if age_x:
        return AgeRating(18, x=True)
    return None if age_rating is None else AgeRating(int(age_rating))


def _require_admin(principal: Principal) -> None:
    """An admin session or the host CLI, never a key nor a device token."""
    if principal.role != "admin" or principal.credential in (
        Credential.API_KEY,
        Credential.DEVICE_TOKEN,
    ):
        raise AttachRefusedError(f"{principal.credential.value}/{principal.role}")


class AutoAttachService:
    def __init__(
        self,
        *,
        events: EventPublisher | None = None,
        rating: RatingService | None = None,
        fresh_auth_s: int = 900,
        tombstones: TombstoneScreen | None = None,
    ) -> None:
        self._tombstones = tombstones
        self._events = events
        self._rating = rating or RatingService(fresh_auth_s=fresh_auth_s)
        self._fresh_auth_s = fresh_auth_s

    # -- the arrival ----------------------------------------------------------------------------
    def arrive(
        self, connection: Connection, incoming: IncomingEdition, *, now: datetime
    ) -> Arrival:
        edition_id = incoming.edition_id
        if self._tombstones is None:  # fail closed: a purged Work must not come back by here
            raise TombstoneScreenMissingError("an arrival is screened by the Work tombstones")
        from_work = _work_of(connection, edition_id)
        # The gate first: whatever follows, a shelf below 18+ does not take it unrated.
        filing = file_new_edition(
            connection, edition_id, incoming.shelf_id, rating=self._rating,
            keep_filed=incoming.keep_filed,
        )  # fmt: skip
        id_origin = _ID_ORIGINS.get(incoming.origin, "source")
        conflicts: list[str] = []
        work_ids: list[ExternalId] = []
        for raw in incoming.ids:
            try:
                clean = normalize_external_id(
                    raw.entity_type, raw.provider, raw.value, id_scheme=raw.id_scheme
                )
            except ExternalIdError:
                conflicts.append("invalid_id")
                continue
            if clean.entity_type == "work":
                work_ids.append(clean)
            elif clean.entity_type == "edition":
                try:
                    add_external_id(connection, edition_id, clean, origin=id_origin)
                except ExternalIdConflictError:
                    conflicts.append("edition_id_held")  # a merge of Editions: L14
            else:
                conflicts.append("unsupported_entity")
        own = [p for p in incoming.fields if p.entity_type == "edition"]
        work_fields = tuple(p for p in incoming.fields if p.entity_type == "work")
        if work_ids and self._tombstones.work_ids(work_ids):
            # A purged Work (or a merged one) does not come back by an identifier it carried:
            # no identifier is recorded for the Work, no attach, nothing of its fields (L14).
            propose_fields(
                connection, {"edition": edition_id}, own, now=now, origin=incoming.origin
            )
            conflicts.append("tombstoned")
            return Arrival(edition_id, from_work, "conflict", filing, conflicts=tuple(conflicts))
        propose_fields(connection, {"edition": edition_id}, own, now=now, origin=incoming.origin)

        held = {e: holder_of(connection, e) for e in work_ids}
        holders = sorted({h for h in held.values() if h is not None and h != from_work})
        if not holders:
            for external in work_ids:
                add_external_id(connection, from_work, external, origin=id_origin)
            self._fields_on(connection, from_work, work_fields, incoming, now)
            return Arrival(edition_id, from_work, "kept", filing, conflicts=tuple(conflicts))
        stay = Arrival(edition_id, from_work, "conflict", filing)
        if len(holders) > 1:
            self._fields_on(connection, from_work, work_fields, incoming, now)
            return _with(stay, conflicts, "ambiguous")
        holder = holders[0]
        matched = next(e for e, h in held.items() if h == holder)
        if not _exists(connection, holder):
            self._fields_on(connection, from_work, work_fields, incoming, now)
            return _with(stay, conflicts, "holder_missing")  # a purged Work: tombstones, L14
        if _same_key_in(connection, edition_id, holder):
            self._fields_on(connection, from_work, work_fields, incoming, now)
            return _with(stay, conflicts, "edition_key")  # a merge of Editions: L14
        if self._raises_age(connection, edition_id, holder):
            self._fields_on(connection, from_work, work_fields, incoming, now)
            attach_id = _journal(
                connection,
                edition_id,
                from_work,
                holder,
                matched,
                "suggested",
                "age_raise",
                {"shelf_id": incoming.shelf_id},
            )
            self._publish(connection, ATTACH_SUGGESTED, attach_id, edition_id, holder, from_work)
            return Arrival(edition_id, from_work, "suggested", filing, attach_id, tuple(conflicts))
        attach_id = new_ulid()
        filing, prior = self._attach(
            connection,
            attach_id,
            edition_id,
            from_work,
            holder,
            work_fields,
            incoming.shelf_id,
            incoming.origin,
            now,
            pending=[e for e in work_ids if held[e] is None],
        )
        _journal_insert(
            connection,
            attach_id,
            edition_id,
            from_work,
            holder,
            matched,
            "attached",
            "external_id",
            prior,
        )
        self._publish(connection, AUTO_ATTACHED, attach_id, edition_id, holder, from_work)
        return Arrival(edition_id, holder, "attached", filing, attach_id, tuple(conflicts))

    # -- the suggestion -------------------------------------------------------------------------
    def plan(self, connection: Connection, attach_id: str) -> AttachPlan:
        """What confirming would make leave a shelf below 18+: computed on the attach done in a
        savepoint that is rolled back. Nothing changes."""
        row = _row(connection, attach_id)
        if row.state != "suggested":
            raise AttachRefusedError(f"not a suggestion: {row.state}")
        if _work_of(connection, row.edition_id) != row.from_work_id:
            raise AttachRefusedError("stale: the Edition moved since the suggestion")
        if not _exists(connection, row.to_work_id):
            raise AttachRefusedError("stale: the Work no longer exists")
        if self._tombstones is None:
            raise TombstoneScreenMissingError("a suggestion is screened by the Work tombstones")
        if self._tombstones.work_ids([ExternalId("work", row.provider, row.value)]):
            raise AttachRefusedError("tombstoned: the identifier was purged since the suggestion")
        savepoint = connection.begin_nested()
        try:
            _carry(connection, row.edition_id, row.from_work_id, now=row.created_at)
            _move(connection, row.edition_id, row.to_work_id)
            _clear_rating(connection, row.edition_id)
            withdrawals = self._rating.withdrawals(connection, work_id=row.to_work_id)
        finally:
            savepoint.rollback()
        return AttachPlan(
            attach_id, row.edition_id, row.to_work_id, withdrawals, _token(attach_id, withdrawals)
        )

    def confirm(
        self,
        connection: Connection,
        attach_id: str,
        *,
        confirm: str,
        principal: Principal,
        now: datetime,
    ) -> AttachPlan:
        """Apply a suggested attach, for the token of the plan the admin read. A parental decision:
        the reserved action `rate`. Returns the removal plan, now to apply (`shelf withdraw`)."""
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["rate"], now=now, fresh_auth_s=self._fresh_auth_s
        )
        if not isinstance(verdict, Allowed):
            raise AttachRefusedError(type(verdict).__name__)
        shown = self.plan(connection, attach_id)
        if not hmac.compare_digest(shown.token, confirm):
            raise AttachPlanStaleError(attach_id)
        row = _row(connection, attach_id)
        prior = dict(row.prior or {})
        _filing, done = self._attach(
            connection,
            attach_id,
            row.edition_id,
            row.from_work_id,
            row.to_work_id,
            (),
            prior.get("shelf_id"),
            "source",
            now,
            pending=[],
        )
        withdrawals = self._rating.withdrawals(connection, work_id=row.to_work_id)
        done["withdrawals"] = [[w.edition_id, w.shelf_id] for w in withdrawals]
        connection.execute(
            sa.update(catalog.edition_attach)
            .where(catalog.edition_attach.c.id == attach_id)
            .values(state="attached", prior=done, decided_at=now)
        )
        _audit(connection, principal, "edition.attach_confirm", row.edition_id, attach_id, now)
        return AttachPlan(attach_id, row.edition_id, row.to_work_id, withdrawals, shown.token)

    def decline(
        self, connection: Connection, attach_id: str, *, principal: Principal, now: datetime
    ) -> None:
        _require_admin(principal)
        row = _row(connection, attach_id)
        if row.state != "suggested":
            raise AttachRefusedError(f"not a suggestion: {row.state}")
        connection.execute(
            sa.update(catalog.edition_attach)
            .where(catalog.edition_attach.c.id == attach_id)
            .values(state="declined", decided_at=now)
        )
        _audit(connection, principal, "edition.attach_decline", row.edition_id, attach_id, now)

    # -- "Séparer" ------------------------------------------------------------------------------
    def separate(
        self, connection: Connection, attach_id: str, *, principal: Principal, now: datetime
    ) -> Detached:
        """Undo an attach exactly: what it changed goes back, what an admin changed since stays.

        A parental decision too (the Edition returns to a Work and a shelf): the reserved action
        `rate`, a fresh session. The rating stamp of the Edition comes back only if the Work it
        returns to was not rated again since the attach, or was rated before the stamp; else the
        Edition is unrated there ("À classer"). Returns what to file again (the sisters the
        confirmation withdrew) and the identifiers it brought, never applied here."""
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["rate"], now=now, fresh_auth_s=self._fresh_auth_s
        )
        if not isinstance(verdict, Allowed):
            raise AttachRefusedError(type(verdict).__name__)
        row = _row(connection, attach_id)
        if row.state != "attached":
            raise AttachRefusedError(f"not attached: {row.state}")
        if _work_of(connection, row.edition_id) != row.to_work_id:
            raise AttachRefusedError("the Edition moved since the attach")
        prior = dict(row.prior or {})
        kept: list[FieldKey] = []
        for written in prior.get("fields", []):
            key = FieldKey(**written["key"])
            if read_field(connection, key) == _stored(written["after"]):
                restore_field(connection, key, _stored(written["before"]))
            else:
                kept.append(key)
        _move(connection, row.edition_id, row.from_work_id)
        _uncarry(connection, prior.get("suggestions", {}))
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == row.edition_id)
            .values(rated_at=_stamp_back(connection, row.from_work_id, prior))
        )
        filing = file_new_edition(
            connection, row.edition_id, prior.get("shelf_id"), rating=self._rating
        )
        refile = tuple(
            Refiling(edition_id, shelf_id)
            for edition_id, shelf_id in prior.get("withdrawals", [])
            if _unfiled(connection, edition_id)
            and self._rating.admission_of(connection, edition_id, shelf_id=shelf_id)
            is Admission.ALLOWED
        )
        pending = tuple(ExternalId("work", p, v) for p, v in prior.get("pending_ids", []))
        connection.execute(
            sa.update(catalog.edition_attach)
            .where(catalog.edition_attach.c.id == attach_id)
            .values(state="undone", decided_at=now)
        )
        _audit(connection, principal, "edition.detach", row.edition_id, attach_id, now)
        return Detached(
            attach_id, row.edition_id, row.from_work_id, filing, tuple(kept), refile, pending
        )

    # -- reads ----------------------------------------------------------------------------------
    def attachments(self, connection: Connection, *, state: str | None = None) -> list[AttachView]:
        t = catalog.edition_attach
        query = sa.select(
            t.c.id,
            t.c.edition_id,
            t.c.from_work_id,
            t.c.to_work_id,
            t.c.state,
            t.c.reason,
            t.c.created_at,
        ).order_by(t.c.id)
        if state is not None:
            query = query.where(t.c.state == state)
        return [AttachView(*r) for r in connection.execute(query)]

    # -- inside ---------------------------------------------------------------------------------
    def _attach(
        self,
        connection: Connection,
        attach_id: str,
        edition_id: str,
        from_work: str,
        to_work: str,
        work_fields: tuple[FieldProposal, ...],
        shelf_id: str | None,
        origin: str,
        now: datetime,
        *,
        pending: list[ExternalId],
    ) -> tuple[Filing, dict[str, Any]]:
        """Move the Edition, write the fields of the Work it brought, file it through the gate;
        with what the undo needs (the `prior` of the journal)."""
        edition = catalog.edition
        before = connection.execute(
            sa.select(edition.c.rated_at, edition.c.shelf_id).where(edition.c.id == edition_id)
        ).one()
        keys = keys_of(list(work_fields), {"work": to_work})
        snapshot = {k: read_field(connection, k) for k in keys}
        origin_work = _work_rating(connection, from_work)
        carried = _carry(connection, edition_id, from_work, now=now)
        _move(connection, edition_id, to_work)
        _clear_rating(connection, edition_id)  # new to this Work: unrated there (EF-BIB-055)
        report = propose_fields(connection, {"work": to_work}, work_fields, now=now, origin=origin)
        filing = file_new_edition(connection, edition_id, shelf_id, rating=self._rating)
        prior = {
            "rated_at": None if before.rated_at is None else before.rated_at.isoformat(),
            "shelf_id": before.shelf_id,
            "origin_work": origin_work,  # its rating when the attach was made
            "suggestions": carried,  # what the attach added: the undo removes those still open
            "fields": [
                {
                    "key": {
                        "entity_type": k.entity_type,
                        "entity_id": k.entity_id,
                        "field": k.field,
                        "lang": k.lang,
                    },
                    "before": _stored_json(snapshot.get(k)),
                    "after": _stored_json(read_field(connection, k)),
                }
                for k in report.written
            ],
            # what the Edition brought for its Work and nobody held: never written by an attach
            "pending_ids": [[e.provider, e.value] for e in pending],
        }
        return filing, prior

    def _fields_on(
        self,
        connection: Connection,
        work_id: str,
        proposals: tuple[FieldProposal, ...],
        incoming: IncomingEdition,
        now: datetime,
    ) -> None:
        propose_fields(connection, {"work": work_id}, proposals, now=now, origin=incoming.origin)

    def _raises_age(self, connection: Connection, edition_id: str, holder: str) -> bool:
        """Would the Edition raise the effective age of the holder? Its age of origin counts: the
        one an admin fixed for it, else the one of the Work it comes from. A holder nobody rated
        has no age to compare with: an Edition with an age of origin waits for an admin."""
        origin = _origin_age(connection, edition_id)
        if origin is None:
            return False
        before = self._rating.effective_age_of(connection, holder)
        return before is None or origin > before

    def _publish(
        self,
        connection: Connection,
        event_type: str,
        attach_id: str,
        edition_id: str,
        work_id: str,
        from_work_id: str,
    ) -> None:
        if self._events is None:
            return
        self._events.publish(
            connection,
            event_type,
            {
                "attach_id": attach_id,
                "edition_id": edition_id,
                "work_id": work_id,
                "from_work_id": from_work_id,
            },
        )


def _with(arrival: Arrival, conflicts: list[str], code: str) -> Arrival:
    return Arrival(
        arrival.edition_id, arrival.work_id, arrival.state, arrival.filing, None, (*conflicts, code)
    )


def _work_of(connection: Connection, edition_id: str) -> str:
    return str(
        connection.execute(
            sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == edition_id)
        ).scalar_one()
    )


def _exists(connection: Connection, work_id: str) -> bool:
    found = connection.execute(sa.select(catalog.work.c.id).where(catalog.work.c.id == work_id))
    return found.first() is not None


def _same_key_in(connection: Connection, edition_id: str, work_id: str) -> bool:
    e = catalog.edition
    mine = connection.execute(sa.select(*(e.c[k] for k in _KEY)).where(e.c.id == edition_id)).one()
    clash = connection.execute(
        sa.select(e.c.id).where(
            e.c.work_id == work_id, *(e.c[k] == v for k, v in zip(_KEY, mine, strict=True))
        )
    ).first()
    return clash is not None


def _move(connection: Connection, edition_id: str, work_id: str) -> None:
    """The Edition and what is tied to its Work by a composite key (bindings, follows), together:
    the foreign keys are checked at the end of the transaction."""
    connection.exec_driver_sql("PRAGMA defer_foreign_keys = ON")
    connection.execute(
        sa.update(catalog.edition).where(catalog.edition.c.id == edition_id).values(work_id=work_id)
    )
    for table in (acquisition.source_binding, acquisition.series_follow):
        connection.execute(
            sa.update(table).where(table.c.edition_id == edition_id).values(work_id=work_id)
        )


def _origin_age(connection: Connection, edition_id: str) -> AgeRating | None:
    """The age the Edition has where it is: its own, else the one of its Work (not the effective
    age of that Work, which its sisters may raise: theirs is not its age)."""
    e, w = catalog.edition, catalog.work
    row = connection.execute(
        sa.select(e.c.age_rating, e.c.age_x, w.c.age_rating, w.c.age_x)
        .join(w, w.c.id == e.c.work_id)
        .where(e.c.id == edition_id)
    ).one()
    own = _age(row[0], row[1])
    return own if own is not None else _age(row[2], row[3])


def _work_rating(connection: Connection, work_id: str) -> dict[str, Any]:
    w = catalog.work
    row = connection.execute(
        sa.select(w.c.age_rating, w.c.age_x, w.c.rated_at).where(w.c.id == work_id)
    ).one()
    return {
        "age_rating": row.age_rating,
        "age_x": bool(row.age_x),
        "rated_at": None if row.rated_at is None else row.rated_at.isoformat(),
    }


def _carry(
    connection: Connection, edition_id: str, from_work: str, *, now: datetime
) -> dict[str, list[Any]]:
    """What the Edition takes with it, as its own suggestions: the age of its Work when it has
    none of its own (`attach`), and the open suggestions of that Work. Returns what it changed."""
    e, w, suggestion = catalog.edition, catalog.work, catalog.age_suggestion
    row = connection.execute(
        sa.select(e.c.age_rating, e.c.age_x, w.c.age_rating, w.c.age_x)
        .join(w, w.c.id == e.c.work_id)
        .where(e.c.id == edition_id)
    ).one()
    wanted: list[tuple[int, bool, str]] = []
    if _age(row[0], row[1]) is None and (age := _age(row[2], row[3])) is not None:
        wanted.append((age.min_age, age.x, "attach"))
    wanted += [
        (int(r.value), bool(r.x), r.provenance)
        for r in connection.execute(
            sa.select(suggestion.c.value, suggestion.c.x, suggestion.c.provenance).where(
                suggestion.c.entity_type == "work",
                suggestion.c.entity_id == from_work,
                suggestion.c.state == "open",
            )
        )
    ]
    added: list[str] = []
    reopened: list[list[str]] = []
    for value, x, provenance in wanted:
        found = connection.execute(
            sa.select(suggestion.c.id, suggestion.c.state).where(
                suggestion.c.entity_type == "edition",
                suggestion.c.entity_id == edition_id,
                suggestion.c.value == value,
                suggestion.c.x == x,
                suggestion.c.provenance == provenance,
            )
        ).first()
        if found is not None:
            if found.state != "open":  # ruled on for this Edition before: it blocks again
                connection.execute(
                    sa.update(suggestion).where(suggestion.c.id == found.id).values(state="open")
                )
                reopened.append([found.id, found.state])
            continue
        suggestion_id = new_ulid()
        connection.execute(
            sa.insert(suggestion).values(
                id=suggestion_id, entity_type="edition", entity_id=edition_id, value=value,
                x=x, provenance=provenance, seen_at=now, state="open",
            )
        )  # fmt: skip
        added.append(suggestion_id)
    return {"added": added, "reopened": reopened}


def _uncarry(connection: Connection, carried: Mapping[str, list[Any]]) -> None:
    """The undo of `_carry`, on what is still open: an admin's ruling since stays."""
    suggestion = catalog.age_suggestion
    open_ = suggestion.c.state == "open"
    if added := carried.get("added", []):
        connection.execute(sa.delete(suggestion).where(suggestion.c.id.in_(added), open_))
    for suggestion_id, state in carried.get("reopened", []):
        connection.execute(
            sa.update(suggestion).where(suggestion.c.id == suggestion_id, open_).values(state=state)
        )


def _stamp_back(connection: Connection, work_id: str, prior: Mapping[str, Any]) -> datetime | None:
    """The rating stamp the Edition had, if it still holds in the Work it returns to."""
    stamp = prior.get("rated_at")
    if stamp is None:
        return None
    at = datetime.fromisoformat(stamp)
    now_rated = _work_rating(connection, work_id)
    if now_rated == prior.get("origin_work"):
        return at  # the Work was not rated again since the attach
    work_at = now_rated["rated_at"]
    if work_at is None or at > datetime.fromisoformat(work_at):
        return at  # rated after its Work: an admin's own decision on it
    return None


def _unfiled(connection: Connection, edition_id: str) -> bool:
    e = catalog.edition
    row = connection.execute(sa.select(e.c.shelf_id).where(e.c.id == edition_id)).first()
    return row is not None and row.shelf_id is None


def _clear_rating(connection: Connection, edition_id: str) -> None:
    connection.execute(
        sa.update(catalog.edition).where(catalog.edition.c.id == edition_id).values(rated_at=None)
    )


def _row(connection: Connection, attach_id: str) -> sa.Row[Any]:
    t = catalog.edition_attach
    row = connection.execute(sa.select(t).where(t.c.id == attach_id)).first()
    if row is None:
        raise LookupError(f"no such attach: {attach_id}")
    return row


def _journal(
    connection: Connection,
    edition_id: str,
    from_work: str,
    to_work: str,
    matched: ExternalId,
    state: str,
    reason: str,
    prior: dict[str, Any],
) -> str:
    attach_id = new_ulid()
    _journal_insert(
        connection, attach_id, edition_id, from_work, to_work, matched, state, reason, prior
    )
    return attach_id


def _journal_insert(
    connection: Connection,
    attach_id: str,
    edition_id: str,
    from_work: str,
    to_work: str,
    matched: ExternalId,
    state: str,
    reason: str,
    prior: dict[str, Any],
) -> None:
    connection.execute(
        sa.insert(catalog.edition_attach).values(
            id=attach_id,
            edition_id=edition_id,
            from_work_id=from_work,
            to_work_id=to_work,
            provider=matched.provider,
            value=matched.value,
            state=state,
            reason=reason,
            prior=prior,
        )
    )


def _token(attach_id: str, withdrawals: tuple[Withdrawal, ...]) -> str:
    lines = [attach_id, *sorted(f"{w.edition_id}:{w.shelf_id}:{w.reason}" for w in withdrawals)]
    return f"{len(withdrawals)}-{hashlib.sha256(chr(10).join(lines).encode()).hexdigest()[:8]}"


def _audit(
    connection: Connection,
    principal: Principal,
    action: str,
    edition_id: str,
    attach_id: str,
    now: datetime,
) -> None:
    connection.execute(
        audit_insert(
            AuditEntry(
                actor_id=principal.actor_id,
                action=action,
                at=now,
                target=f"edition:{edition_id};attach:{attach_id}",
                session_id=principal.session_id,
                auth_time=principal.auth_time,
            )
        )
    )
