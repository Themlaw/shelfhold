"""`relink`: a new source binding for a Work that already exists (EF-BIB-029, EF-BIB-035).

A series moved to another source in the connected service (Suwayomi's migration) is the same
Work: the admin attaches the new binding to it instead of creating a second one. Nothing the
Edition holds is fetched again, only the numbers it lacks; the Team and its aliases follow the
binding (I27); and an Edition that is *new* for a Work that was rated never enters a shelf below
18+ by itself: it waits unfiled until an admin rates it (EF-BIB-055, `RatingService.admission_of`).

The caller owns the transaction. The command is the host's: a session of an admin or the local
CLI, never a key (nothing here is reserved, so it asks `_require_admin` itself).
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Connection
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.catalog.application.dto import BindingRef, EditionSpec
from shelfhold.catalog.application.places import place_key
from shelfhold.catalog.application.rating import RatingService
from shelfhold.catalog.application.service import CatalogService
from shelfhold.catalog.domain.age import Admission
from shelfhold.identity.public import AuditEntry, Credential, Principal, audit_insert
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog

__all__ = [
    "ListedUnit",
    "RelinkRefusedError",
    "RelinkRequest",
    "RelinkResult",
    "Relinker",
]


class RelinkRefusedError(Exception):
    """Only an admin session or the host's CLI may relink."""


@dataclass(frozen=True)
class ListedUnit:
    """One release the new source lists: its number as the source writes it, and its order."""

    raw_number: str | None
    source_order: int = 0


@dataclass(frozen=True)
class RelinkRequest:
    work_id: str
    plugin_id: str
    source_key: str
    remote_id: str
    listed: tuple[ListedUnit, ...] = ()
    lang: str | None = None  # None: the Edition of the binding replaced, else the only one
    replaces_binding_id: str | None = None
    translation_type: str | None = None  # a new Edition: default, that of the Edition it follows
    media: str | None = None
    memo: dict[str, object] | None = None
    memo_version: int | None = None


@dataclass(frozen=True)
class RelinkResult:
    binding_id: str
    edition_id: str
    edition_created: bool
    missing: tuple[ListedUnit, ...]  # what the Edition lacks: the only numbers to fetch
    to_rate: bool  # the Edition waits in "À classer" (EF-BIB-055)
    carried_teams: tuple[str, ...]  # raw labels whose alias moved to the new binding


class Relinker:
    def __init__(
        self, catalog_for: Callable[[Connection], CatalogService], rating: RatingService
    ) -> None:
        self._catalog_for = catalog_for
        self._rating = rating

    def relink(
        self, connection: Connection, request: RelinkRequest, *, principal: Principal,
        now: datetime,
    ) -> RelinkResult:  # fmt: skip
        _require_admin(principal)
        work_rated = _work_rated(connection, request.work_id)
        replaced = _replaced(connection, request)
        books = self._catalog_for(connection)
        base = _base_edition(connection, request, replaced)
        edition_id, created = self._edition(books, request, base)
        if replaced is not None:
            _retire(connection, replaced.id)
        binding_id = _bind(connection, request, edition_id, replaced, now)
        carried = _carry_teams(
            connection, books, replaced, BindingRef(binding_id, edition_id), created, now
        )
        to_rate = False
        if created:
            to_rate = self._file_new_edition(connection, edition_id, base, work_rated)
        missing = _missing(connection, edition_id, request.listed)
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=principal.actor_id, action="relink", at=now,
                    target=f"work:{request.work_id}", session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        )  # fmt: skip
        return RelinkResult(binding_id, edition_id, created, missing, to_rate, carried)

    # -- the Edition ----------------------------------------------------------------------------
    @staticmethod
    def _edition(
        books: CatalogService, request: RelinkRequest, base: "_Base | None"
    ) -> tuple[str, bool]:
        if request.lang is None:
            if base is None:
                raise LookupError("the Work has no Edition: give a language")
            return base.id, False
        ref = books.ensure_edition(
            EditionSpec(
                work_id=request.work_id, lang=request.lang,
                translation_type=request.translation_type
                or (base.translation_type if base else "fan"),
                media=request.media or (base.media if base else "images"),
            )
        )  # fmt: skip
        return ref.id, ref.created

    def _file_new_edition(
        self, connection: Connection, edition_id: str, base: "_Base | None", work_rated: bool
    ) -> bool:
        """A new Edition goes where its siblings are only if the shelf would admit it; else it
        stays unfiled and is reported for "À classer". `rated_at` stays empty: only an admin
        rating sets it (EF-BIB-055). Returns whether it waits for a rating."""
        shelf_id = base.shelf_id if base is not None else None
        waiting = work_rated  # a Work already rated, and an Edition nobody has rated
        if shelf_id is not None:
            verdict = self._rating.admission_of(connection, edition_id, shelf_id=shelf_id)
            if verdict is Admission.ALLOWED:
                connection.execute(
                    sa.update(catalog.edition)
                    .where(catalog.edition.c.id == edition_id)
                    .values(shelf_id=shelf_id)
                )
            else:
                waiting = True
        return waiting


@dataclass(frozen=True)
class _Binding:
    id: str
    edition_id: str


@dataclass(frozen=True)
class _Base:
    """The Edition a new one follows: its key parts and the shelf it is filed on."""

    id: str
    translation_type: str
    media: str
    shelf_id: str | None


def _require_admin(principal: Principal) -> None:
    if principal.role != "admin" or principal.credential in (
        Credential.API_KEY,
        Credential.DEVICE_TOKEN,
    ):  # a key, whatever its scope, is never the admin at the console
        raise RelinkRefusedError(f"{principal.credential.value}/{principal.role}")


def _work_rated(connection: Connection, work_id: str) -> bool:
    row = connection.execute(
        sa.select(catalog.work.c.rated_at).where(catalog.work.c.id == work_id)
    ).first()
    if row is None:
        raise LookupError(f"no such Work: {work_id}")
    return row.rated_at is not None


def _replaced(connection: Connection, request: RelinkRequest) -> _Binding | None:
    if request.replaces_binding_id is None:
        return None
    binding = acquisition.source_binding
    row = connection.execute(
        sa.select(binding.c.id, binding.c.edition_id).where(
            binding.c.id == request.replaces_binding_id, binding.c.work_id == request.work_id
        )
    ).first()
    if row is None:
        raise LookupError(f"no such binding for this Work: {request.replaces_binding_id}")
    return _Binding(row.id, row.edition_id)


def _base_edition(
    connection: Connection, request: RelinkRequest, replaced: _Binding | None
) -> _Base | None:
    edition = catalog.edition
    query = sa.select(
        edition.c.id, edition.c.translation_type, edition.c.media, edition.c.shelf_id
    ).order_by(edition.c.shelf_id.is_(None), edition.c.id)
    if replaced is not None:
        query = query.where(edition.c.id == replaced.edition_id)
    else:
        query = query.where(edition.c.work_id == request.work_id)
    rows = connection.execute(query).all()
    if not rows:
        return None
    if request.lang is None and replaced is None and len(rows) > 1:
        raise ValueError("the Work has several Editions: give a language")
    first = rows[0]
    return _Base(first.id, first.translation_type, first.media, first.shelf_id)


def _retire(connection: Connection, binding_id: str) -> None:
    binding = acquisition.source_binding
    connection.execute(
        sa.update(binding)
        .where(binding.c.id == binding_id)
        .values(active=False, disabled_reason="manual")
    )


def _bind(
    connection: Connection, request: RelinkRequest, edition_id: str,
    replaced: _Binding | None, now: datetime,
) -> str:  # fmt: skip
    item, binding = acquisition.source_item, acquisition.source_binding
    item_id = connection.execute(
        sa.select(item.c.id).where(
            item.c.plugin_id == request.plugin_id,
            item.c.source_key == request.source_key,
            item.c.remote_id == request.remote_id,
        )
    ).scalar()
    if item_id is None:
        item_id = new_ulid()
        connection.execute(
            sa.insert(item).values(
                id=item_id, plugin_id=request.plugin_id, source_key=request.source_key,
                remote_id=request.remote_id, memo=request.memo or {},
                memo_version=request.memo_version or 1, last_seen=now,
            )
        )  # fmt: skip
    elif connection.execute(
        sa.select(binding.c.id).where(
            binding.c.edition_id == edition_id, binding.c.source_item_id == item_id
        )
    ).first():
        raise ValueError("this source is already bound to that Edition")
    top = connection.execute(
        sa.select(sa.func.max(binding.c.priority)).where(
            binding.c.edition_id == edition_id, binding.c.active
        )
    ).scalar()
    binding_id = new_ulid()
    connection.execute(
        sa.insert(binding).values(
            id=binding_id, work_id=request.work_id, edition_id=edition_id,
            source_item_id=item_id, priority=(top or 0) + 1, active=True,
        )
    )  # fmt: skip
    connection.execute(  # a followed series has its follow row; the first numbered look sets it
        sqlite_insert(acquisition.series_follow)
        .values(edition_id=edition_id, work_id=request.work_id)
        .on_conflict_do_nothing(index_elements=["edition_id"])
    )
    return binding_id


def _carry_teams(
    connection: Connection, books: CatalogService, replaced: _Binding | None,
    binding: BindingRef, created: bool, now: datetime,
) -> tuple[str, ...]:  # fmt: skip
    """The aliases of the old binding move to the new one: same Team in the same Edition (I27),
    the Team of that label in a new Edition; the priority of the Edition follows the labels."""
    if replaced is None:
        return ()
    alias, team = catalog.team_alias, catalog.team
    labels = list(
        connection.execute(
            sa.select(alias.c.label)
            .where(alias.c.source_binding_id == replaced.id)
            .order_by(alias.c.first_seen_at, alias.c.label)
        ).scalars()
    )
    for label in labels:
        books.resolve_team(binding, label, now)
    if created and replaced.edition_id != binding.edition_id:
        old = books.team_priority(replaced.edition_id)
        rows = connection.execute(
            sa.select(team.c.id, team.c.edition_id, team.c.label).where(
                team.c.edition_id.in_([replaced.edition_id, binding.edition_id])
            )
        ).all()
        label_of = {r.id: r.label for r in rows if r.edition_id == replaced.edition_id}
        here = {r.label: r.id for r in rows if r.edition_id == binding.edition_id}
        ordered = [here[label_of[i]] for i in old if label_of.get(i) in here]
        if ordered:
            books.set_team_priority(binding.edition_id, ordered)
    return tuple(labels)


def _missing(
    connection: Connection, edition_id: str, listed: tuple[ListedUnit, ...]
) -> tuple[ListedUnit, ...]:
    unit = catalog.unit
    held: set[str] = set()
    for row in connection.execute(
        sa.select(
            unit.c.track,
            unit.c.number_segment,
            unit.c.number_norm,
            unit.c.part,
            unit.c.source_order,
        ).where(unit.c.edition_id == edition_id)
    ):
        held.add(
            f"unnumbered:{row.source_order}" if row.track == "unnumbered"
            else f"{row.track}|{row.number_segment}|{row.number_norm}|{row.part}"
        )  # fmt: skip
    return tuple(u for u in listed if place_key(u.raw_number, u.source_order) not in held)
