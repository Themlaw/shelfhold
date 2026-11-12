"""Requests: ask, approve in one gesture, refuse with a reason, arrive (EF-USR-010, ENF-OBS-003).

A request names its target in one of four ways and only becomes a Work at approval. The service
owns the states, the visibility (what `effective_visibility` says of the person who asked) and the
audit; what lives in other contexts (the source, the connected service, the shelves) is reached
through `Ports`, which the application wires and the tests replace.

Approval is the reserved action `request.approve` (an admin session, fresh, holding the right to
rate, never a key): the service checks it again, as every second door does. A child's request needs
an age, which is the rating of the book (B2): applied at once when the Work exists, and recorded on
the request and applied when the Work appears when it does not (a followed series is made by the
next cycle). The decision is claimed in its own transaction before anything is asked of a source,
so a replay or a second worker cannot follow twice; a failure puts the request back to pending.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.domain.request_target import (
    InvalidTargetError,
    RequestTarget,
    approval_refusal,
    can_transition,
    child_may_ask,
    needs_age,
    parse_target,
    pick_edition,
)
from shelfhold.catalog.public import RatingRefusedError, RatingService
from shelfhold.events.bus import EventBus, PublishedEvent
from shelfhold.identity.public import (
    RESERVED_ACTIONS,
    Allowed,
    AuditEntry,
    Credential,
    Principal,
    audit_insert,
    authorize_reserved,
    effective_visibility,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, identity
from shelfhold.sdk.age import AgeRating

__all__ = [
    "MAX_PENDING",
    "ApprovalPreview",
    "Estimate",
    "Ports",
    "RequestRefusedError",
    "RequestService",
    "RequestView",
    "Requester",
    "SourceRef",
]

MAX_PENDING = 20  # open requests per profile: a child's list is not a place to pile things up
MAX_REASON = 500
MAX_TITLE = 300
_LIST = 200
request = identity.request


class RequestRefusedError(Exception):
    """A refusal with a stable code (`request.<code>` in the error catalogue)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Requester:
    user_id: str
    role: str
    locale: str = "en"


@dataclass(frozen=True)
class SourceRef:
    """A remote result a child was shown: what a request on `source_ref` points at."""

    source_key: str
    remote_id: str
    title: str
    memo: dict[str, Any]


@dataclass(frozen=True)
class Estimate:
    units: int
    size_bytes: int


@dataclass(frozen=True)
class RequestView:
    id: str
    state: str
    target_kind: str
    title: str | None  # None: the admin looking at a private request of an adult
    reject_reason: str | None
    visibility: str
    requester_id: str
    requester_role: str
    created_at: datetime
    updated_at: datetime
    needs_age: bool = False


@dataclass(frozen=True)
class ApprovalPreview:
    units: int
    size_bytes: int
    needs_age: bool
    confirmation_required: bool
    edition_id: str | None
    shelf_id: str | None
    resolved: bool


class Ports(Protocol):
    """What an approval asks of the rest of the application."""

    def kind_of(self, source_key: str) -> str:
        """`manga` (followed in the connected service) or anything else (acquired at once)."""
        ...

    def estimate(self, source_key: str, remote_id: str) -> Estimate: ...

    def follow(
        self, source_key: str, remote_id: str, *, requester_user_id: str | None, visibility: str
    ) -> None: ...

    def acquire(
        self, source_key: str, remote_id: str, *, requester_user_id: str | None, visibility: str
    ) -> str | None:
        """The Work that now exists for this result."""
        ...

    def proposed_shelf(self, edition_id: str) -> str | None: ...

    def file(self, edition_id: str, shelf_id: str, *, approver_id: str) -> None: ...


@dataclass(frozen=True)
class _Resolved:
    work_id: str | None
    source: tuple[str, str] | None  # source key, remote identifier
    title: str | None
    found: bool


def _has_forbidden(text: str) -> bool:
    return any(unicodedata.category(c) in {"Cc", "Cf", "Cs", "Co", "Cn"} for c in text)


def clean_reason(reason: object) -> str:
    if not isinstance(reason, str):
        raise RequestRefusedError("invalid_reason")
    text = reason.strip()
    if not text or len(text) > MAX_REASON or _has_forbidden(text):
        raise RequestRefusedError("invalid_reason")
    return text


def _is_admin_session(principal: Principal) -> bool:
    return principal.credential is Credential.SESSION and principal.role == "admin"


class RequestService:
    def __init__(
        self, engine: Engine, clock: Clock, ports: Ports, *, bus: EventBus | None = None,
        rating: RatingService | None = None, fresh_auth_s: int = 900,
    ) -> None:  # fmt: skip
        self._fresh_auth_s = fresh_auth_s
        self._engine = engine
        self._clock = clock
        self._ports = ports
        self._bus = bus or EventBus(clock)
        self._rating = rating or RatingService()

    # -- asking -------------------------------------------------------------------------------

    def create_from_ref(self, who: Requester, ref: SourceRef) -> RequestView:
        """A request on the remote result of a search; the one door of a child."""
        with self._engine.begin() as connection:
            owner = self._owner(connection, who.user_id)
            item = _upsert_item(connection, ref, self._clock.now())
            return self._insert(connection, who.user_id, owner, RequestTarget("source_ref", item))

    def create(self, who: Requester, target: RequestTarget) -> RequestView:
        """A request on a Work, an ISBN or a text. A child has no such door (`kind_not_allowed`,
        the same whatever the Work: no identifier of hers says whether a title is there)."""
        if target.kind == "source_ref":
            raise RequestRefusedError("kind_not_allowed")  # only `create_from_ref` makes one
        with self._engine.begin() as connection:
            owner = self._owner(connection, who.user_id)
            if needs_age(owner.role):
                raise RequestRefusedError("kind_not_allowed")
            try:
                checked = parse_target(target.kind, target.value)
            except InvalidTargetError:
                raise RequestRefusedError("invalid_target") from None
            if checked.kind == "work_id" and not _work_exists(connection, checked.value):
                raise RequestRefusedError("not_found")
            return self._insert(connection, who.user_id, owner, checked)

    def _owner(self, connection: Connection, user_id: str) -> _Owner:
        row = connection.execute(
            sa.select(identity.user.c.role, identity.user.c.visibility).where(
                identity.user.c.id == user_id
            )
        ).first()
        if row is None:
            raise RequestRefusedError("not_found")
        return _Owner(row.role, row.visibility)

    def _insert(
        self, connection: Connection, user_id: str, owner: _Owner, target: RequestTarget
    ) -> RequestView:
        if target.kind == "source_ref" and not child_may_ask(target.kind):  # pragma: no cover
            raise RequestRefusedError("kind_not_allowed")
        same = connection.execute(
            sa.select(request.c.id).where(
                request.c.requester_user_id == user_id,
                request.c.target_kind == target.kind,
                request.c.target_value == target.value,
                request.c.state == "pending",
            )
        ).first()
        if same is not None:  # asking twice for the same thing is one request
            return self._view(connection, same.id)
        open_count = connection.execute(
            sa.select(sa.func.count()).where(
                request.c.requester_user_id == user_id, request.c.state == "pending"
            )
        ).scalar_one()
        if open_count >= MAX_PENDING:
            raise RequestRefusedError("too_many_pending")
        now = self._clock.now()
        request_id = new_ulid()
        connection.execute(
            sa.insert(request).values(
                id=request_id, requester_user_id=user_id, target_kind=target.kind,
                target_value=target.value, state="pending", created_at=now, updated_at=now,
                visibility=effective_visibility(owner.role, owner.visibility),
            )
        )  # fmt: skip
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=user_id,
                    action="request.created",
                    at=now,
                    target=f"request:{request_id}",
                )
            )
        )
        return self._view(connection, request_id)

    # -- reading --------------------------------------------------------------------------------

    def get(self, user_id: str, request_id: str) -> RequestView:
        """One of her own requests; another's is exactly an absent one."""
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(request.c.id).where(
                    request.c.id == request_id, request.c.requester_user_id == user_id
                )
            ).first()
            if row is None:
                raise RequestRefusedError("not_found")
            return self._view(connection, request_id)

    def mine(self, user_id: str) -> list[RequestView]:
        with self._engine.connect() as connection:
            ids = connection.execute(
                sa.select(request.c.id)
                .where(request.c.requester_user_id == user_id)
                .order_by(request.c.created_at.desc(), request.c.id.desc())
                .limit(_LIST)
            ).scalars()
            return [self._view(connection, i) for i in ids]

    def queue(self, principal: Principal) -> list[RequestView]:
        """What waits for an admin, oldest first. The title of a private request of an adult is
        withheld (the operation, not the title); unmasking is its own audited door."""
        self._admin_session(principal)
        with self._engine.connect() as connection:
            ids = connection.execute(
                sa.select(request.c.id)
                .where(request.c.state == "pending")
                .order_by(request.c.created_at, request.c.id)
                .limit(_LIST)
            ).scalars()
            return [self._view(connection, i, for_admin=True) for i in ids]

    def preview(self, request_id: str, principal: Principal) -> ApprovalPreview:
        self._admin_session(principal)
        with self._engine.connect() as connection:
            row = self._pending(connection, request_id)
            owner = self._owner(connection, row.requester_user_id)
            resolved = self._resolve(connection, row)
            estimate = self._estimate(resolved)
            work = resolved.work_id
            edition = self._edition(connection, work, row.requester_user_id) if work else None
            shelf = self._ports.proposed_shelf(edition) if edition else None
            rated = _rated(connection, work) if work else False
            return ApprovalPreview(
                estimate.units, estimate.size_bytes,
                needs_age=needs_age(owner.role) and not rated,
                confirmation_required=approval_refusal(
                    requester_role="member", age=None, already_rated=True, units=estimate.units,
                    size_bytes=estimate.size_bytes, confirmed=False,
                ) is not None,
                edition_id=edition, shelf_id=shelf, resolved=resolved.found,
            )  # fmt: skip

    # -- deciding -------------------------------------------------------------------------------

    def approve(
        self, request_id: str, principal: Principal, *, age: int | None = None, x: bool = False,
        shelf_id: str | None = None, confirm_size: bool = False,
    ) -> RequestView:  # fmt: skip
        verdict = authorize_reserved(
            principal, RESERVED_ACTIONS["request.approve"], now=self._clock.now(),
            fresh_auth_s=self._fresh_auth_s,
        )  # fmt: skip
        if not isinstance(verdict, Allowed):
            raise RequestRefusedError("not_allowed")
        rating = _rating_of(age, x)
        with self._engine.connect() as connection:
            row = self._pending(connection, request_id)
            owner = self._owner(connection, row.requester_user_id)
            resolved = self._resolve(connection, row)
            if not resolved.found:
                raise RequestRefusedError("unresolved")
            estimate = self._estimate(resolved)
            work = resolved.work_id
            refusal = approval_refusal(
                requester_role=owner.role, age=age if rating is None else rating.min_age,
                already_rated=bool(work) and _rated(connection, work or ""),
                units=estimate.units, size_bytes=estimate.size_bytes, confirmed=confirm_size,
            )  # fmt: skip
            if refusal is not None:
                raise RequestRefusedError(refusal)
        now = self._clock.now()
        visibility = _stored(self._engine, request_id).visibility
        audit_id = self._claim(request_id, principal, rating, now, shelf_id)
        try:
            self._materialize(request_id, row, resolved, rating, principal, shelf_id, visibility)
        except BaseException:
            self._revert(request_id, audit_id)
            raise
        with self._engine.connect() as connection:
            return self._view(connection, request_id)

    def _claim(
        self, request_id: str, principal: Principal, rating: AgeRating | None, now: datetime,
        shelf_id: str | None,
    ) -> str:  # fmt: skip
        """Phase 1: the decision and its audit line, in one transaction. Whoever loses the race
        finds the request no longer pending."""
        audit_id = new_ulid()
        with self._engine.begin() as connection:
            changed = connection.execute(
                sa.update(request)
                .where(request.c.id == request_id, request.c.state == "pending")
                .values(
                    state="approved", decided_by=principal.user_id, decided_at=now, updated_at=now,
                    approved_age=None if rating is None else rating.min_age,
                    approved_x=bool(rating and rating.x), shelf_id=shelf_id,
                )
            )  # fmt: skip
            if changed.rowcount != 1:
                raise RequestRefusedError("not_pending")
            connection.execute(
                sa.insert(identity.audit).values(
                    id=audit_id, actor_id=principal.actor_id, action="request.approved", at=now,
                    target=f"request:{request_id}"
                    + ("" if rating is None else f";age:{rating.min_age}{'x' if rating.x else ''}"),
                    session_id=principal.session_id, auth_time=principal.auth_time,
                )
            )  # fmt: skip
        return audit_id

    def _revert(self, request_id: str, audit_id: str) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(request)
                .where(request.c.id == request_id, request.c.state == "approved")
                .values(
                    state="pending", decided_by=None, decided_at=None, approved_age=None,
                    approved_x=False, shelf_id=None, updated_at=self._clock.now(),
                )
            )  # fmt: skip
            connection.execute(sa.delete(identity.audit).where(identity.audit.c.id == audit_id))

    def _materialize(
        self, request_id: str, row: Any, resolved: _Resolved, rating: AgeRating | None,
        principal: Principal, shelf_id: str | None, visibility: str,
    ) -> None:  # fmt: skip
        work = resolved.work_id
        if resolved.source is not None:
            source_key, remote_id = resolved.source
            if self._ports.kind_of(source_key) == "manga":
                self._ports.follow(
                    source_key, remote_id, requester_user_id=row.requester_user_id,
                    visibility=visibility,
                )  # fmt: skip
            else:
                work = self._ports.acquire(
                    source_key, remote_id, requester_user_id=row.requester_user_id,
                    visibility=visibility,
                )  # fmt: skip
        if work is None:  # a followed series: the Work comes with the next cycle
            return
        now = self._clock.now()
        with self._engine.begin() as connection:
            if rating is not None and not _rated(connection, work):
                try:
                    self._rating.rate(connection, work, rating, principal=principal, now=now)
                except RatingRefusedError:
                    raise RequestRefusedError("not_allowed") from None
            edition = self._edition(connection, work, row.requester_user_id)
        shelf = shelf_id or (self._ports.proposed_shelf(edition) if edition else None)
        if edition is not None and shelf is not None:
            self._ports.file(edition, shelf, approver_id=principal.user_id)
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(request)
                .where(request.c.id == request_id)
                .values(work_id=work, edition_id=edition, shelf_id=shelf)
            )

    def reject(self, request_id: str, principal: Principal, reason: object) -> RequestView:
        self._admin_session(principal)
        text = clean_reason(reason)
        return self._close(request_id, principal, "rejected", "request.rejected", text)

    def mark_not_found(self, request_id: str, principal: Principal) -> RequestView:
        self._admin_session(principal)
        return self._close(request_id, principal, "not_found", "request.not_found", None)

    def cancel(self, user_id: str, request_id: str) -> RequestView:
        """Her own pending request, by her alone."""
        with self._engine.begin() as connection:
            row = connection.execute(
                sa.select(request.c.state).where(
                    request.c.id == request_id, request.c.requester_user_id == user_id
                )
            ).first()
            if row is None:
                raise RequestRefusedError("not_found")
            self._move(connection, request_id, "cancelled", "request.cancelled", user_id, None)
            return self._view(connection, request_id)

    def _close(
        self, request_id: str, principal: Principal, state: str, action: str, reason: str | None
    ) -> RequestView:
        with self._engine.begin() as connection:
            self._move(
                connection, request_id, state, action, principal.actor_id, reason,
                decided_by=principal.user_id, principal=principal,
            )  # fmt: skip
            return self._view(connection, request_id)

    def _move(
        self, connection: Connection, request_id: str, state: str, action: str, actor: str,
        reason: str | None, *, decided_by: str | None = None, principal: Principal | None = None,
    ) -> None:  # fmt: skip
        current = connection.execute(
            sa.select(request.c.state).where(request.c.id == request_id)
        ).first()
        if current is None:
            raise RequestRefusedError("not_found")
        if not can_transition(current.state, state):
            raise RequestRefusedError("not_pending")
        now = self._clock.now()
        values: dict[str, Any] = {"state": state, "updated_at": now, "reject_reason": reason}
        if decided_by is not None:
            values.update(decided_by=decided_by, decided_at=now)
        changed = connection.execute(
            sa.update(request)
            .where(request.c.id == request_id, request.c.state == current.state)
            .values(**values)
        )
        if changed.rowcount != 1:
            raise RequestRefusedError("not_pending")
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=actor,
                    action=action,
                    at=now,
                    target=f"request:{request_id}",
                    session_id=principal.session_id if principal else None,
                    auth_time=principal.auth_time if principal else None,
                )
            )
        )

    # -- arrival --------------------------------------------------------------------------------

    def arrived(self, work_id: str) -> int:
        """A book of this Work was filed: the approved requests that asked for it are `arrived`,
        the age chosen at approval is applied to a Work nobody rated, and each requester hears of
        it, in the view of the request (private, or household). A replay finds nothing to do."""
        published: list[PublishedEvent] = []
        now = self._clock.now()
        with self._engine.begin() as connection:
            for row in self._waiting_for(connection, work_id):
                if row.approved_age is not None:
                    self._rating.rate_decided(
                        connection, work_id,
                        AgeRating(int(row.approved_age), x=bool(row.approved_x)),
                        request_id=row.id, decided_by=row.decided_by or "", now=now,
                    )  # fmt: skip
                edition = self._edition(connection, work_id, row.requester_user_id)
                changed = connection.execute(
                    sa.update(request)
                    .where(request.c.id == row.id, request.c.state == "approved")
                    .values(
                        state="arrived", arrived_at=now, updated_at=now, work_id=work_id,
                        edition_id=row.edition_id or edition,
                    )
                )  # fmt: skip
                if changed.rowcount == 1:
                    published.append(
                        self._bus.publish(
                            connection,
                            "ops.request_arrived",
                            {"request_id": row.id},
                            actor_id=row.requester_user_id,
                            visibility=row.visibility,
                        )
                    )
        self._bus.deliver(published)
        return len(published)

    @staticmethod
    def _waiting_for(connection: Connection, work_id: str) -> list[Any]:
        item, binding = acquisition.source_item, acquisition.source_binding
        pending = acquisition.pending_source_item
        via_source = (
            sa.select(sa.literal(1))
            .select_from(
                pending.join(
                    item,
                    sa.and_(
                        item.c.source_key == pending.c.source_key,
                        item.c.remote_id == pending.c.series_ref,
                    ),
                ).join(binding, binding.c.source_item_id == item.c.id)
            )
            .where(
                pending.c.id == request.c.target_value,
                request.c.target_kind == "source_ref",
                binding.c.work_id == work_id,
            )
            .exists()
        )
        return list(
            connection.execute(
                sa.select(request).where(
                    request.c.state == "approved",
                    sa.or_(request.c.work_id == work_id, via_source),
                )
            )
        )

    # -- internals ------------------------------------------------------------------------------

    @staticmethod
    def _admin_session(principal: Principal) -> None:
        if not _is_admin_session(principal):
            raise RequestRefusedError("not_allowed")

    @staticmethod
    def _pending(connection: Connection, request_id: str) -> Any:
        row = connection.execute(sa.select(request).where(request.c.id == request_id)).first()
        if row is None:
            raise RequestRefusedError("not_found")
        if row.state != "pending":
            raise RequestRefusedError("not_pending")
        return row

    def _estimate(self, resolved: _Resolved) -> Estimate:
        if resolved.source is None:
            return Estimate(0, 0)  # a Work the library holds: nothing to fetch
        return self._ports.estimate(*resolved.source)

    @staticmethod
    def _edition(connection: Connection, work_id: str | None, requester_user_id: str) -> str | None:
        if work_id is None:
            return None
        locale = connection.execute(
            sa.select(identity.user.c.locale).where(identity.user.c.id == requester_user_id)
        ).scalar_one_or_none()
        editions = connection.execute(
            sa.select(catalog.edition.c.id, catalog.edition.c.lang)
            .where(catalog.edition.c.work_id == work_id)
            .order_by(catalog.edition.c.id)
        ).all()
        return pick_edition([(e.id, e.lang) for e in editions], locale or "en")

    @staticmethod
    def _resolve(connection: Connection, row: Any) -> _Resolved:
        """The target as the library knows it now: the Work it is, or the remote result it names.
        A typed text or an ISBN only resolves to one Work the catalogue already holds."""
        kind, value = row.target_kind, row.target_value
        if kind == "work_id":
            title = connection.execute(
                sa.select(catalog.work.c.title).where(catalog.work.c.id == value)
            ).scalar_one_or_none()
            return _Resolved(value if title is not None else None, None, title, title is not None)
        if kind == "source_ref":
            item = connection.execute(
                sa.select(acquisition.pending_source_item).where(
                    acquisition.pending_source_item.c.id == value
                )
            ).first()
            if item is None:
                return _Resolved(None, None, None, False)
            return _Resolved(None, (item.source_key, item.series_ref), item.title, True)
        if kind == "isbn":
            found = _work_by_isbn(connection, value)
            return _Resolved(found, None, None, found is not None)
        found = _work_by_title(connection, value)
        return _Resolved(found, None, None, found is not None)

    def _view(
        self, connection: Connection, request_id: str, *, for_admin: bool = False
    ) -> RequestView:
        row = connection.execute(
            sa.select(request, identity.user.c.role.label("owner_role"))
            .select_from(
                request.join(identity.user, identity.user.c.id == request.c.requester_user_id)
            )
            .where(request.c.id == request_id)
        ).one()
        masked = for_admin and row.visibility == "private" and not needs_age(row.owner_role)
        title = None if masked else _title(connection, row)
        return RequestView(
            id=row.id, state=row.state, target_kind=row.target_kind, title=title,
            reject_reason=row.reject_reason, visibility=row.visibility,
            requester_id=row.requester_user_id, requester_role=row.owner_role,
            created_at=row.created_at, updated_at=row.updated_at,
            needs_age=row.state == "pending" and needs_age(row.owner_role),
        )  # fmt: skip


@dataclass(frozen=True)
class _Owner:
    role: str
    visibility: str | None


def _stored(engine: Engine, request_id: str) -> Any:
    with engine.connect() as connection:
        return connection.execute(
            sa.select(request.c.visibility).where(request.c.id == request_id)
        ).one()


def _rating_of(age: int | None, x: bool) -> AgeRating | None:
    if age is None:
        return None
    try:
        return AgeRating(18, x=True) if x else AgeRating(age)
    except (ValueError, TypeError):
        raise RequestRefusedError("choose_age") from None


def _rated(connection: Connection, work_id: str) -> bool:
    return bool(
        connection.execute(
            sa.select(catalog.work.c.rated_at).where(catalog.work.c.id == work_id)
        ).scalar_one_or_none()
    )


def _work_exists(connection: Connection, work_id: str) -> bool:
    return (
        connection.execute(sa.select(catalog.work.c.id).where(catalog.work.c.id == work_id)).first()
        is not None
    )


def _work_by_isbn(connection: Connection, isbn: str) -> str | None:
    ext = catalog.external_id
    rows = connection.execute(
        sa.select(ext.c.entity_type, ext.c.entity_id).where(
            ext.c.provider == "isbn", ext.c.value == isbn, ext.c.state == "current"
        )
    ).all()
    works: set[str] = set()
    for entity_type, entity_id in rows:
        if entity_type == "work":
            works.add(entity_id)
        elif entity_type == "edition":
            found = connection.execute(
                sa.select(catalog.edition.c.work_id).where(catalog.edition.c.id == entity_id)
            ).scalar_one_or_none()
            if found:
                works.add(found)
    return next(iter(works)) if len(works) == 1 else None


def _work_by_title(connection: Connection, text: str) -> str | None:
    rows = (
        connection.execute(
            sa.select(catalog.work.c.id)
            .where(sa.func.lower(catalog.work.c.title) == text.lower())
            .limit(2)
        )
        .scalars()
        .all()
    )
    return rows[0] if len(rows) == 1 else None


def _upsert_item(connection: Connection, ref: SourceRef, now: datetime) -> str:
    item = acquisition.pending_source_item
    title = " ".join(ref.title.split())[:MAX_TITLE] or "-"
    found = connection.execute(
        sa.select(item.c.id).where(
            item.c.source_key == ref.source_key, item.c.series_ref == ref.remote_id
        )
    ).first()
    if found is not None:
        connection.execute(
            sa.update(item)
            .where(item.c.id == found.id)
            .values(title=title, memo=ref.memo, fetched_at=now)
        )
        return str(found.id)
    item_id = new_ulid()
    connection.execute(
        sa.insert(item).values(
            id=item_id,
            source_key=ref.source_key,
            series_ref=ref.remote_id,
            title=title,
            memo=ref.memo,
            fetched_at=now,
        )
    )
    return item_id


def _title(connection: Connection, row: Any) -> str | None:
    kind, value = row.target_kind, row.target_value
    if kind == "source_ref":
        return connection.execute(
            sa.select(acquisition.pending_source_item.c.title).where(
                acquisition.pending_source_item.c.id == value
            )
        ).scalar_one_or_none()
    if kind == "work_id":
        return connection.execute(
            sa.select(catalog.work.c.title).where(catalog.work.c.id == value)
        ).scalar_one_or_none()
    return str(value)
