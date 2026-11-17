"""Importable reading lists: bulk resolution, one review screen, a reading shelf (EF-USR-020).

A list is plain text, one wanted book per line, with an ISBN if the person has one. The import:

- reads the text as untrusted (`parse_list`: bounded, never interpreted);
- resolves every line against the catalogue **only** (the ISBN, else the title folded as everywhere:
  `fold_title`). It never asks a source: no search is made at import, for anyone, and a child's list
  is never searched (D22), whoever imports it;
- puts what the profile is already shown on a **reading shelf** (a profile's own list of L18a), in
  the order of the file; what is not there becomes a request of that profile (an ISBN or a text),
  made in the same transaction, so that a list is whole or absent;
- keeps the batch (`request_list_import`, `request_list_item`) so that the admin reads all of it on
  **one** screen, and decides each request with the ordinary approval (an age for a child's).

Who imports: an adult for themselves (a member's list stays as private as their requests); an admin
for a child, which is a parental decision (the reserved action `reading_shelf.attach`, checked by
the door). A child never imports. A key or a device token never does: an import is made by a
session, or by the host's command line. The shelf of a child is built with what she is shown, so it
never holds above her ceiling whoever made it. A batch is read by the admin, and by its owner for
their own: another profile's batch is an absent one.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.requests import (
    RequestRefusedError,
    RequestService,
    work_by_isbn,
)
from shelfhold.acquisition.domain.request_list import ListLine, ListRefusedError, parse_list
from shelfhold.acquisition.domain.request_target import (
    InvalidTargetError,
    RequestTarget,
    needs_age,
    parse_target,
)
from shelfhold.catalog.public import Collections, CollectionViewer, fold_title
from shelfhold.identity.public import (
    AuditEntry,
    Credential,
    Principal,
    audit_insert,
    reader_scope,
)
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, identity

__all__ = [
    "MAX_LISTED_PENDING",
    "ImportDone",
    "InvalidListError",
    "ListItemView",
    "ListReview",
    "ListSummary",
    "RequestListService",
]

MAX_LISTED_PENDING = 200  # open requests of one profile, imports included
_NAME_MAX = 200
request = identity.request
list_import = identity.request_list_import
list_item = identity.request_list_item


@dataclass(frozen=True)
class ImportDone:
    id: str
    in_library: int  # lines put on the reading shelf
    requested: int  # lines that became a request
    annotated: int  # lines that ask for a precise annotated edition (a warning, not a refusal)
    collection_id: str | None


@dataclass(frozen=True)
class ListItemView:
    position: int
    text: str | None  # None: the list of another adult, which an admin reads as counts only
    isbn: str | None
    outcome: str
    annotated: bool
    request_id: str | None
    request_state: str | None
    needs_age: bool
    in_library: bool


@dataclass(frozen=True)
class ListReview:
    id: str
    name: str
    user_id: str
    user_role: str
    created_at: datetime
    collection_id: str | None
    items: list[ListItemView]
    in_library: int
    requested: int
    annotated: int


@dataclass(frozen=True)
class ListSummary:
    id: str
    name: str
    user_id: str
    user_role: str
    created_at: datetime
    requested: int
    in_library: int


class InvalidListError(RequestRefusedError):
    """The list is not read (`request.invalid_list`); `reason` says why, never with a piece."""

    def __init__(self, reason: str) -> None:
        super().__init__("invalid_list")
        self.reason = reason


class RequestListService:
    def __init__(self, engine: Engine, clock: Clock, requests: RequestService) -> None:
        self._engine = engine
        self._clock = clock
        self._requests = requests

    # -- importing --------------------------------------------------------------------------------

    def import_list(
        self, principal: Principal, raw: str | bytes, *, name: str | None = None,
        for_user_id: str | None = None, host: bool = False,
    ) -> ImportDone:  # fmt: skip
        """Import a list for oneself, or (an admin) for a child. `RequestRefusedError` carries a
        code of `request.*`; a list that is not read is `InvalidListError`, with its `reason`."""
        if principal.credential is not Credential.SESSION and not host:
            raise RequestRefusedError("not_allowed")
        if needs_age(principal.role):  # a child asks among her search results, never with a list
            raise RequestRefusedError("kind_not_allowed")
        try:
            lines = parse_list(raw)
        except ListRefusedError as refused:
            raise InvalidListError(refused.reason) from None
        title = " ".join((name or "").split())[:_NAME_MAX] or "Reading list"
        with write_transaction(self._engine) as connection:
            subject, role = self._subject(connection, principal, for_user_id, host=host)
            return self._import(connection, principal, subject, role, lines, title)

    def _subject(
        self, connection: Connection, principal: Principal, for_user_id: str | None, *, host: bool
    ) -> tuple[str, str]:
        """The profile the list is for. A caller's own, or a child's (by an admin: a member makes
        no list for another). The host's command line has no account: its own list is the first
        admin's."""
        if for_user_id is None or for_user_id == principal.user_id:
            if host:
                found = connection.execute(
                    sa.select(identity.user.c.id)
                    .where(identity.user.c.role == "admin")
                    .order_by(identity.user.c.created_at, identity.user.c.id)
                ).scalar()
                if found is None:
                    raise RequestRefusedError("not_found")
                return str(found), "admin"
            return principal.user_id, principal.role
        if principal.role != "admin":
            raise RequestRefusedError("not_allowed")
        role = connection.execute(
            sa.select(identity.user.c.role).where(identity.user.c.id == for_user_id)
        ).scalar()
        if role is None:
            raise RequestRefusedError("not_found")
        if not needs_age(str(role)):  # an adult's requests are theirs: nobody files them for them
            raise RequestRefusedError("not_allowed")
        return for_user_id, str(role)

    def _import(
        self, connection: Connection, principal: Principal, subject: str, role: str,
        lines: list[ListLine], name: str,
    ) -> ImportDone:  # fmt: skip
        viewer = CollectionViewer(subject, False, reader_scope(connection, subject))
        works = _resolve(connection, lines)
        shown = Collections.shown_to(connection, viewer, [w for w in works.values() if w])
        now = self._clock.now()
        import_id = new_ulid()
        shelf: list[tuple[int, str]] = []
        rows: list[dict[str, Any]] = []
        requested = 0
        for line in lines:
            work = works[line.position]
            row: dict[str, Any] = {
                "id": new_ulid(), "import_id": import_id, "position": line.position,
                "line_text": (line.text or line.isbn or "")[:300], "isbn": line.isbn,
                "annotated": line.annotated, "request_id": None, "work_id": work,
            }  # fmt: skip
            if work is not None and work in shown:
                shelf.append((line.position, work))
                row["outcome"] = "in_library"
            else:
                kind, value = ("isbn", line.isbn) if line.isbn else ("free_text", line.text)
                target = _target(kind, value or "")
                view = self._requests.create_listed(
                    connection, subject, target, cap=MAX_LISTED_PENDING
                )
                row["outcome"] = "requested"
                row["request_id"] = view.id
                row["work_id"] = None  # the match is not the subject's to know (a hidden Work)
                requested += 1
            rows.append(row)
        collection_id = Collections.attach_reading_shelf(connection, viewer, name, shelf)
        connection.execute(
            sa.insert(list_import).values(
                id=import_id, user_id=subject, created_by=_account(principal), name=name,
                collection_id=collection_id, created_at=now,
            )
        )  # fmt: skip
        connection.execute(sa.insert(list_item), rows)
        annotated = sum(1 for line in lines if line.annotated)
        attached = needs_age(role)  # a shelf made for a child: the parental decision
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=principal.actor_id,
                    action="reading_shelf.attached" if attached else "request.list_imported",
                    at=now,
                    target=f"request_list:{import_id}",
                    session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        )
        return ImportDone(import_id, len(shelf), requested, annotated, collection_id)

    # -- reading ----------------------------------------------------------------------------------

    def review(self, principal: Principal, import_id: str) -> ListReview:
        """The whole batch on one screen: an admin session reads any batch, a profile its own.
        The lines of another adult's list are not shown to an admin (a private request is hidden
        from them as well), only the counts."""
        _reader(principal)
        with self._engine.connect() as connection:
            head = connection.execute(
                sa.select(list_import, identity.user.c.role.label("role"))
                .select_from(
                    list_import.join(identity.user, identity.user.c.id == list_import.c.user_id)
                )
                .where(list_import.c.id == import_id)
            ).first()  # fmt: skip
            if head is None or not _may_read(principal, head.user_id):
                raise RequestRefusedError("not_found")  # absent, or not hers: the same answer
            hidden = (
                principal.role == "admin"
                and not _owns(principal, head.user_id)
                and not needs_age(head.role)
            )
            rows = connection.execute(
                sa.select(list_item, request.c.state.label("state"))
                .select_from(
                    list_item.join(request, request.c.id == list_item.c.request_id, isouter=True)
                )
                .where(list_item.c.import_id == import_id)
                .order_by(list_item.c.position)
            ).all()  # fmt: skip
        items = [
            ListItemView(
                position=r.position, text=None if hidden else r.line_text,
                isbn=None if hidden else r.isbn, outcome=r.outcome, annotated=r.annotated,
                request_id=r.request_id, request_state=r.state,
                needs_age=r.state == "pending" and needs_age(head.role),
                in_library=r.outcome == "in_library",
            )
            for r in rows
        ]  # fmt: skip
        return ListReview(
            id=head.id, name=head.name, user_id=head.user_id, user_role=head.role,
            created_at=head.created_at, collection_id=head.collection_id, items=items,
            in_library=sum(1 for i in items if i.in_library),
            requested=sum(1 for i in items if not i.in_library),
            annotated=sum(1 for i in items if i.annotated),
        )  # fmt: skip

    def imports(self, principal: Principal, *, limit: int = 50) -> list[ListSummary]:
        """The lists an admin may open (those of children and their own), the newest first; a
        profile's own for anybody else."""
        _reader(principal)
        counts = (
            sa.select(
                list_item.c.import_id,
                sa.func.sum(sa.case((list_item.c.outcome == "requested", 1), else_=0)).label("r"),
                sa.func.sum(sa.case((list_item.c.outcome == "in_library", 1), else_=0)).label("l"),
            )
            .group_by(list_item.c.import_id)
            .subquery()
        )
        query = (
            sa.select(list_import, identity.user.c.role.label("role"), counts.c.r, counts.c.l)
            .select_from(
                list_import.join(identity.user, identity.user.c.id == list_import.c.user_id).join(
                    counts, counts.c.import_id == list_import.c.id, isouter=True
                )
            )
            .order_by(list_import.c.created_at.desc(), list_import.c.id.desc())
            .limit(max(1, min(limit, 200)))
        )  # fmt: skip
        if principal.role == "admin":
            query = query.where(
                sa.or_(
                    list_import.c.user_id == principal.user_id,
                    identity.user.c.role.not_in(("admin", "member")),
                )
            )
        else:
            query = query.where(list_import.c.user_id == principal.user_id)
        with self._engine.connect() as connection:
            return [
                ListSummary(
                    r.id, r.name, r.user_id, r.role, r.created_at, int(r.r or 0), int(r.l or 0)
                )
                for r in connection.execute(query)
            ]

    def profile_id(self, username: str) -> str | None:
        """The profile of a name, for the host's command line (None: nobody is called that)."""
        with self._engine.connect() as connection:
            found = connection.execute(
                sa.select(identity.user.c.id).where(identity.user.c.username == username)
            ).scalar()
        return None if found is None else str(found)


def _reader(principal: Principal) -> None:
    if principal.credential is not Credential.SESSION or needs_age(principal.role):
        raise RequestRefusedError("not_allowed")


def _owns(principal: Principal, user_id: str) -> bool:
    return principal.user_id == user_id


def _may_read(principal: Principal, user_id: str) -> bool:
    return principal.role == "admin" or _owns(principal, user_id)


def _account(principal: Principal) -> str | None:
    return principal.user_id if principal.credential is Credential.SESSION else None


def _target(kind: str, value: str) -> RequestTarget:
    try:
        return parse_target(kind, value)
    except InvalidTargetError:  # an ISBN-only line was checked; a title is a title or nothing
        raise RequestRefusedError("invalid_target") from None


def _resolve(connection: Connection, lines: list[ListLine]) -> dict[int, str | None]:
    """The one Work each line names in the catalogue: its ISBN first, else its title folded (the
    same rule as an arrival). Zero or several is no Work, never a guess. One pass over the titles,
    whatever the number of lines. Nothing is asked of a source."""
    wanted = {fold_title(line.text) for line in lines if line.text} - {""}
    by_title: dict[str, list[str]] = {}
    if wanted:
        for row in connection.execute(sa.select(catalog.work.c.id, catalog.work.c.title)):
            key = fold_title(row.title)
            if key in wanted:
                by_title.setdefault(key, []).append(str(row.id))
    found: dict[int, str | None] = {}
    for line in lines:
        work = work_by_isbn(connection, line.isbn) if line.isbn else None
        if work is None and line.text:
            titled = by_title.get(fold_title(line.text), [])
            work = titled[0] if len(titled) == 1 else None
        found[line.position] = work
    return found
