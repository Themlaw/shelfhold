"""Collections and reading lists, read as a profile (EF-BIB-016, EF-USR-006, M4 L18a).

A collection with no owner is the household's: an admin makes it, every profile reads what of it she
may see. A collection with an owner is that profile's own list: private by default, read by its
owner and by an admin and by nobody else. A list that is not the reader's is exactly a list that
does not exist: the same absent answer, whatever the identifier.

What a reader is shown of a list is made of the Works she may see, and of nothing else:
- an item is shown when its Work is visible to her (`queries.visible_work_ids`, the one filter,
  which for a child holds the shelves open to her: `shelf.allowed_profiles`, D-P3); an adult is not
  narrowed (as everywhere in the app);
- the count, the order, the cover and the title come out of the shown items only: places are
  numbered again from 1 among them (no gap says where a hidden one was), the cover is the first
  shown item's, and the name of a household list that holds something hidden from her is withheld
  (a name such as "…, volume 9" could spoil it);
- a household list that holds only hidden Works is absent for her, as is the one she cannot read.

A Work that is removed leaves its places (`ON DELETE CASCADE`); one that is merged moves them to the
survivor (`work_merge.move_collection_items`).
"""

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import sqlalchemy as sa

from shelfhold.catalog.application.queries import (
    ReaderScope,
    first_visible_covers,
    visible_work_ids,
)
from shelfhold.catalog.domain.reading_list import ReadingListFile, title_key
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog

__all__ = [
    "MAX_ITEMS",
    "CollectionFullError",
    "CollectionNotFoundError",
    "CollectionNotPrivateError",
    "CollectionNotYoursError",
    "CollectionSummary",
    "CollectionView",
    "CollectionViewer",
    "Collections",
    "ImportResult",
    "ItemView",
    "Unresolved",
]

MAX_ITEMS: Final = 5000
MAX_LISTED: Final = 200
Scope = Literal["shared", "profile"]
Kind = Literal["collection", "reading_list"]
_CHUNK = 500


@dataclass(frozen=True)
class CollectionViewer:
    """Who reads or writes: the profile, whether she is an admin, and her scope (her ceiling)."""

    user_id: str
    is_admin: bool
    scope: ReaderScope


class CollectionNotFoundError(LookupError):
    """Absent, or not the reader's: the same error, so nothing tells the two apart."""


class CollectionNotYoursError(PermissionError):
    """The reader may read this list but not change it (a household list, for a non-admin)."""


class CollectionFullError(ValueError):
    pass


class CollectionNotPrivateError(ValueError):
    """A household collection has nothing to approve: it is not a profile's private list."""


@dataclass(frozen=True)
class ItemView:
    id: str
    position: int  # numbered again from 1 among the items shown
    work_id: str
    title: str
    number: str | None
    cover_ref: str | None


@dataclass(frozen=True)
class CollectionSummary:
    id: str
    kind: str
    name: str | None  # None: withheld, it could tell of a Work she may not see
    scope: Scope
    mine: bool
    count: int
    cover_ref: str | None
    created_at: datetime


@dataclass(frozen=True)
class CollectionView:
    summary: CollectionSummary
    items: list[ItemView]


@dataclass(frozen=True)
class Unresolved:
    index: int  # the line of the file, from 0
    series: str
    number: str
    reason: Literal["not_found", "ambiguous"]


@dataclass(frozen=True)
class ImportResult:
    collection: CollectionSummary
    imported: int
    unresolved: list[Unresolved]


@dataclass(frozen=True)
class _Row:
    id: str
    kind: str
    name: str
    owner_id: str | None
    created_at: datetime


def _chunks(values: Sequence[str]) -> list[list[str]]:
    return [list(values[i : i + _CHUNK]) for i in range(0, len(values), _CHUNK)]


def _readable(viewer: CollectionViewer, row: _Row) -> bool:
    return row.owner_id is None or row.owner_id == viewer.user_id or viewer.is_admin


def _row(connection: sa.Connection, collection_id: str) -> _Row | None:
    c = catalog.collection.c
    found = connection.execute(
        sa.select(c.id, c.kind, c.name, c.owner_id, c.created_at).where(c.id == collection_id)
    ).first()
    return None if found is None else _Row(*found)


def _shown_works(
    connection: sa.Connection, viewer: CollectionViewer, work_ids: Sequence[str]
) -> dict[str, str | None]:
    """The Works of `work_ids` this reader may see, each with the cover of the first Edition she
    may see (None when it has none). Absent and hidden are the same: not in the answer."""
    unique = sorted(set(work_ids))
    seen = visible_work_ids(connection, viewer.scope, unique)
    covers = first_visible_covers(connection, viewer.scope, sorted(seen))
    return {work: covers[work][1] if work in covers else None for work in sorted(seen)}


def _titles(connection: sa.Connection, work_ids: Sequence[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    work = catalog.work.c
    for chunk in _chunks(sorted(set(work_ids))):
        found.update(
            {
                str(r.id): str(r.title)
                for r in connection.execute(
                    sa.select(work.id, work.title).where(work.id.in_(chunk))
                )
            }
        )
    return found


def _items_of(connection: sa.Connection, collection_ids: Sequence[str]) -> dict[str, list[Any]]:
    item = catalog.collection_item.c
    by_collection: dict[str, list[Any]] = defaultdict(list)
    for chunk in _chunks(collection_ids):
        for row in connection.execute(
            sa.select(item.id, item.collection_id, item.position, item.work_id, item.number)
            .where(item.collection_id.in_(chunk))
            .order_by(item.collection_id, item.position)
        ):
            by_collection[str(row.collection_id)].append(row)
    return by_collection


def _present(
    viewer: CollectionViewer,
    row: _Row,
    items: list[Any],
    shown: dict[str, str | None],
    titles: dict[str, str],
) -> CollectionView | None:
    """What this reader is shown of one collection, or None when it is not hers to see."""
    kept = [i for i in items if str(i.work_id) in shown]
    mine = row.owner_id == viewer.user_id
    shared = row.owner_id is None
    if not viewer.scope.unrestricted and shared and items and not kept:
        return None  # a household list of nothing but what she may not see does not exist for her
    hidden = len(items) - len(kept)
    withheld = not viewer.scope.unrestricted and shared and hidden > 0
    views = [
        ItemView(
            id=str(i.id), position=n, work_id=str(i.work_id), title=titles.get(str(i.work_id), ""),
            number=i.number, cover_ref=shown[str(i.work_id)],
        )
        for n, i in enumerate(kept, start=1)
    ]  # fmt: skip
    cover = next((v.cover_ref for v in views if v.cover_ref is not None), None)
    summary = CollectionSummary(
        id=row.id, kind=row.kind, name=None if withheld else row.name,
        scope="shared" if shared else "profile", mine=mine, count=len(views), cover_ref=cover,
        created_at=row.created_at,
    )  # fmt: skip
    return CollectionView(summary, views)


class Collections:
    def __init__(self, engine: sa.Engine) -> None:
        self._engine = engine

    # -- reads ---------------------------------------------------------------------------------

    def list_for(
        self, viewer: CollectionViewer, *, limit: int = 50, offset: int = 0
    ) -> list[CollectionSummary]:
        """The collections this reader may open, the household's first, then by name."""
        c = catalog.collection.c
        limit = max(0, min(limit, MAX_LISTED))
        with self._engine.connect() as connection:
            query = sa.select(c.id, c.kind, c.name, c.owner_id, c.created_at)
            if not viewer.is_admin:
                query = query.where(sa.or_(c.owner_id.is_(None), c.owner_id == viewer.user_id))
            rows = [
                _Row(*r)
                for r in connection.execute(
                    query.order_by(c.owner_id.is_not(None), sa.func.lower(c.name), c.id)
                )
            ]
            items = _items_of(connection, [r.id for r in rows])
            every = [str(i.work_id) for found in items.values() for i in found]
            shown = _shown_works(connection, viewer, every)
            titles = _titles(connection, list(shown))
            found_views = [_present(viewer, r, items.get(r.id, []), shown, titles) for r in rows]
        return [v.summary for v in found_views if v is not None][offset : offset + limit]

    def get(self, viewer: CollectionViewer, collection_id: str) -> CollectionView:
        with self._engine.connect() as connection:
            return self._view(connection, viewer, collection_id)

    def _view(
        self, connection: sa.Connection, viewer: CollectionViewer, collection_id: str
    ) -> CollectionView:
        row = _row(connection, collection_id)
        if row is None or not _readable(viewer, row):
            raise CollectionNotFoundError(collection_id)
        items = _items_of(connection, [row.id]).get(row.id, [])
        shown = _shown_works(connection, viewer, [str(i.work_id) for i in items])
        view = _present(viewer, row, items, shown, _titles(connection, list(shown)))
        if view is None:
            raise CollectionNotFoundError(collection_id)
        return view

    # -- writes --------------------------------------------------------------------------------

    def create(self, viewer: CollectionViewer, *, name: str, kind: Kind, scope: Scope) -> str:
        if scope == "shared" and not viewer.is_admin:
            raise CollectionNotYoursError(scope)
        collection_id = new_ulid()
        with write_transaction(self._engine) as connection:
            self._insert(connection, viewer, collection_id, name, kind, scope, "manual")
        return collection_id

    @staticmethod
    def _insert(
        connection: sa.Connection,
        viewer: CollectionViewer,
        collection_id: str,
        name: str,
        kind: str,
        scope: str,
        source: str,
    ) -> None:
        connection.execute(
            sa.insert(catalog.collection).values(
                id=collection_id, kind=kind, name=name, source=source,
                owner_id=None if scope == "shared" else viewer.user_id,
            )
        )  # fmt: skip

    def _writable(
        self, connection: sa.Connection, viewer: CollectionViewer, collection_id: str
    ) -> _Row:
        """A list is written by its owner; a household one by an admin. Everything else is the
        absent answer, on every door: a list that is not hers to read, one that she may read but
        that belongs to someone else (an admin reads a profile's list, never writes in it), and a
        household list that holds only what she may not see. Only a household list she can see, and
        may not change, is a 403."""
        row = _row(connection, collection_id)
        if row is None or not _readable(viewer, row):
            raise CollectionNotFoundError(collection_id)
        if row.owner_id is not None and row.owner_id != viewer.user_id:
            raise CollectionNotFoundError(collection_id)
        if row.owner_id is None and not viewer.is_admin:
            items = _items_of(connection, [row.id]).get(row.id, [])
            shown = _shown_works(connection, viewer, [str(i.work_id) for i in items])
            if _present(viewer, row, items, shown, {}) is None:
                raise CollectionNotFoundError(collection_id)
            raise CollectionNotYoursError(collection_id)
        return row

    def add_item(
        self, viewer: CollectionViewer, collection_id: str, work_id: str, number: str | None = None
    ) -> ItemView:
        with write_transaction(self._engine) as connection:
            self._writable(connection, viewer, collection_id)
            shown = _shown_works(connection, viewer, [work_id])
            if work_id not in shown:  # hidden and absent are the same answer
                raise CollectionNotFoundError(work_id)
            item = catalog.collection_item.c
            last, count = connection.execute(
                sa.select(sa.func.coalesce(sa.func.max(item.position), -1), sa.func.count()).where(
                    item.collection_id == collection_id
                )
            ).one()
            if count >= MAX_ITEMS:
                raise CollectionFullError(collection_id)
            item_id = new_ulid()
            # an item added is a list the admin has not seen: his word for Kavita is taken back
            connection.execute(
                sa.update(catalog.collection)
                .where(catalog.collection.c.id == collection_id)
                .values(public_approved_at=None)
            )
            connection.execute(
                sa.insert(catalog.collection_item).values(
                    id=item_id, collection_id=collection_id, position=int(last) + 1,
                    work_id=work_id, number=number or None,
                )
            )  # fmt: skip
            title = _titles(connection, [work_id])[work_id]
            every = _items_of(connection, [collection_id]).get(collection_id, [])
            visible = _shown_works(connection, viewer, [str(i.work_id) for i in every])
            place = sum(1 for i in every if str(i.work_id) in visible)  # as the read numbers them
        return ItemView(item_id, place, work_id, title, number or None, shown[work_id])

    def remove_item(self, viewer: CollectionViewer, collection_id: str, item_id: str) -> None:
        with write_transaction(self._engine) as connection:
            self._writable(connection, viewer, collection_id)
            item = catalog.collection_item.c
            found = connection.execute(
                sa.select(item.work_id).where(
                    item.id == item_id, item.collection_id == collection_id
                )
            ).scalar()
            if found is None or str(found) not in _shown_works(connection, viewer, [str(found)]):
                raise CollectionNotFoundError(item_id)
            connection.execute(sa.delete(catalog.collection_item).where(item.id == item_id))

    def set_public_approval(self, collection_id: str, approved: bool, *, now: datetime) -> bool:
        """The admin's word that a profile's private list may be promoted in Kavita (M4 L18b), or
        its withdrawal. The caller has checked that it is an admin; a household collection is
        already seen by everyone and has nothing to approve."""
        with write_transaction(self._engine) as connection:
            row = _row(connection, collection_id)
            if row is None:
                raise CollectionNotFoundError(collection_id)
            if row.owner_id is None:
                raise CollectionNotPrivateError(collection_id)
            connection.execute(
                sa.update(catalog.collection)
                .where(catalog.collection.c.id == collection_id)
                .values(public_approved_at=now if approved else None)
            )
        return approved

    def delete(self, viewer: CollectionViewer, collection_id: str) -> None:
        with write_transaction(self._engine) as connection:
            self._writable(connection, viewer, collection_id)
            connection.execute(
                sa.delete(catalog.collection).where(catalog.collection.c.id == collection_id)
            )

    @staticmethod
    def shown_to(
        connection: sa.Connection, viewer: CollectionViewer, work_ids: Sequence[str]
    ) -> set[str]:
        """Which of `work_ids` this reader is shown (the rule of the lists: the catalogue filter,
        and for a child an Edition filed on a shelf open to her)."""
        return set(_shown_works(connection, viewer, work_ids))

    @staticmethod
    def attach_reading_shelf(
        connection: sa.Connection, owner: CollectionViewer, name: str,
        entries: Sequence[tuple[int, str]],
    ) -> str:  # fmt: skip
        """A reading list that belongs to `owner`, made by someone else (an admin for a child), in
        the caller's transaction. Only what the owner is shown goes on it, in the order given (a
        Work twice is kept once): the shelf of a child never holds above her ceiling, whoever made
        it. `entries` are `(position, work_id)`."""
        collection_id = new_ulid()
        Collections._insert(
            connection, owner, collection_id, name.strip()[:200] or "Reading list",
            "reading_list", "profile", "manual",
        )  # fmt: skip
        shown = _shown_works(connection, owner, [work for _p, work in entries])
        seen: set[str] = set()
        for position, work_id in entries:
            if work_id not in shown or work_id in seen or len(seen) >= MAX_ITEMS:
                continue
            seen.add(work_id)
            connection.execute(
                sa.insert(catalog.collection_item).values(
                    id=new_ulid(), collection_id=collection_id, position=position,
                    work_id=work_id,
                )
            )  # fmt: skip
        return collection_id

    @staticmethod
    def land_on_reading_shelf(
        connection: sa.Connection, owner: CollectionViewer, collection_id: str,
        entries: Sequence[tuple[int, str]],
    ) -> int:  # fmt: skip
        """M6 L21 (EF-USR-020): what an arrival puts on a reading shelf, in the caller's
        transaction. Only what the owner is shown lands on it (the rule of the import: the shelf
        of a child never holds above her ceiling, whoever made it), a Work already on it is kept
        once, and the place of each entry is kept when it is free. A shelf that is gone, no
        longer the owner's, or full refuses nothing: the line stays « arrivée, à classer ».
        Returns how many Works landed."""
        row = _row(connection, collection_id)
        if row is None or row.owner_id != owner.user_id:
            return 0
        item = catalog.collection_item.c
        shown = _shown_works(connection, owner, [work for _p, work in entries])
        places = {
            int(r.position)
            for r in connection.execute(
                sa.select(item.position).where(item.collection_id == collection_id)
            )
        }
        held = {
            str(r.work_id)
            for r in connection.execute(
                sa.select(item.work_id).where(item.collection_id == collection_id)
            )
        }
        placed = 0
        for position, work_id in entries:
            if work_id not in shown or work_id in held or len(places) >= MAX_ITEMS:
                continue
            place = position if position not in places else max(places) + 1
            connection.execute(
                sa.insert(catalog.collection_item).values(
                    id=new_ulid(), collection_id=collection_id, position=place,
                    work_id=work_id,
                )
            )  # fmt: skip
            places.add(place)
            held.add(work_id)
            placed += 1
        if placed:
            # an item added is a list the admin has not seen: his word for Kavita is taken back
            connection.execute(
                sa.update(catalog.collection)
                .where(catalog.collection.c.id == collection_id)
                .values(public_approved_at=None)
            )
        return placed

    # -- the import of a reading-list file -----------------------------------------------------

    def import_list(
        self, viewer: CollectionViewer, parsed: ReadingListFile, *, scope: Scope, name: str | None
    ) -> ImportResult:
        """Make a reading list of the file: the order of the file is the order of the list; a line
        that names no Work this reader may see, or more than one, is listed and left out, never
        invented or guessed. Her own list resolves only against what she may see, so a hidden Work
        is a line that was not found, exactly like one the catalogue never had."""
        if scope == "shared" and not viewer.is_admin:
            raise CollectionNotYoursError(scope)
        with write_transaction(self._engine) as connection:
            wanted = {title_key(b.series) for b in parsed.books}
            candidates = _candidates(connection, wanted)
            shown = _shown_works(
                connection, viewer, [w for works in candidates.values() for w in works]
            )
            places: list[tuple[int, str, str]] = []
            unresolved: list[Unresolved] = []
            for index, book in enumerate(parsed.books):
                matches = [w for w in candidates.get(title_key(book.series), []) if w in shown]
                if len(matches) == 1:
                    places.append((index, matches[0], book.number))
                else:
                    unresolved.append(
                        Unresolved(
                            index, book.series, book.number, "ambiguous" if matches else "not_found"
                        )
                    )
            collection_id = new_ulid()
            self._insert(
                connection, viewer, collection_id,
                (name or parsed.name or "Reading list").strip()[:200] or "Reading list",
                "reading_list", scope, "cbl",
            )  # fmt: skip
            for index, work_id, number in places:
                connection.execute(
                    sa.insert(catalog.collection_item).values(
                        id=new_ulid(), collection_id=collection_id, position=index,
                        work_id=work_id, number=number or None,
                    )
                )  # fmt: skip
            view = self._view(connection, viewer, collection_id)
        return ImportResult(view.summary, len(places), unresolved)


def _candidates(connection: sa.Connection, wanted: set[str]) -> dict[str, list[str]]:
    """The Works whose title, folded, is one of `wanted`: a title is the name of a Work, and a
    line of a file names it the way it was written (case, spaces, width forms)."""
    work = catalog.work.c
    found: dict[str, list[str]] = defaultdict(list)
    for row in connection.execute(sa.select(work.id, work.title).order_by(work.id)):
        key = title_key(str(row.title))
        if key in wanted:
            found[key].append(str(row.id))
    return found
