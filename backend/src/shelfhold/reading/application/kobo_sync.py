"""The Kobo sync service: what one device is given, and what changed since it was last seen
(M6 L17a, EF-INT-012, D20, EF-USR-006).

The protocol is not documented by the manufacturer: the surface is the minimal one the device
needs, written from the form its requests have. A book is an **Edition** (the thing a shelf
holds, a cover and a downloadable file belong to); the identifier the protocol speaks is derived
from it (`entitlement_of`: a UUID, stable, that says nothing of the database), and a tag is a
**shelf** the profile may be named with, holding the entitlements filed on it.

The scope is the profile of the device token, read from the database at each request
(`reader_scope`), and the one filter of the catalogue is a condition of every query: a page, a
tag and a count are made of what the profile may see, and nothing is filtered afterwards.

The pages of one sync are separate requests, and the catalogue changes under them: a ceiling
lowered, a shelf closed or a book hidden between two pages must not change what one sync says.
The first page of a sync therefore **freezes one snapshot per device** (the `in_progress`
document of `kobo_sync_point`, dated of that instant): the walk it serves (the books as they
were, each marked new or changed), what the device already holds, the tags, the reading states.
The pages serve from that walk — re-verifying the scope at each page, so a book hidden mid-sync
is consumed, not served — and the sync point written at the completion holds **what the device
was given**, not what the last page could see: what it was served and may no longer be seen is
announced as `DeletedEntitlements` at the last page and stays in the point (the next delta says
it again), and what became visible on the way was never served, is not in the point, and the
next delta announces it. A full enumeration started over a device that already holds books (no
token, an unreadable one, or the cursor of a flight another request replaced) also takes back
what it holds and may no longer see: the entitlements it held when the sync started are
announced as deleted at the last page if the profile may no longer see them, and leave the
point. The point is dated of the instant of the first page: whatever happened on the way is the
next delta's to say. The snapshot is persisted at each page under the write lock, its served
books merged with what the lock finds: two pages of one flight never lose each other's work,
and the completion reads the row in the transaction that writes it.

What the device already has is the **sync point**, one row per device (`kobo_sync_point`): the
entitlements and the tags of its last completed sync, each book with the fingerprint of what may
change. A sync without a continuation token serves everything as new; one with the completion
token of its last sync answers the difference — new books (`NewEntitlements`), titles that
changed (`ChangedProductMetadata`), books gone or hidden (`DeletedEntitlements`, the identifier
the device holds, never a title), shelves that changed (`NewTagging`, `ChangedTagging`,
`DeletedTagging`) and reading positions moved since (`ChangedReadingState`). The continuation
token is opaque (`x-kobo-synctoken`), the answer says `X-Kobo-sync: continue` while a page
follows, and a token that is not one of ours starts a full sync again — the device is never stuck.

No request leaves: the store proxy is off, nothing here talks to the manufacturer's servers, and
the resources the initialization names are the routes of this server only.
"""

import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.catalog.public import ReaderScope, shelf_open_to, visible_editions_clause
from shelfhold.identity.public import Principal, reader_scope
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog, export, identity, reading

__all__ = ["KoboSync", "SyncPage", "entitlement_of"]

PAGE: Final = 25  # entitlements by sync page: the device asks again until it is told to stop
_CHUNK: Final = 400  # identifiers by `IN`: a library holds more books than one statement names
_MAX_TOKEN: Final = 512  # a continuation token longer than this is not one of ours
_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "shelfhold-kobo")  # the ids the protocol speaks
_TAG_TYPE: Final = "UserTag"


def entitlement_of(edition_id: str) -> str:
    """The identifier the protocol speaks for an Edition: derived, stable, saying nothing of the
    database (a UUID, as the device expects)."""
    return str(uuid.uuid5(_NAMESPACE, f"edition:{edition_id}"))


def _tag_of(shelf_id: str) -> str:
    return str(uuid.uuid5(_NAMESPACE, f"shelf:{shelf_id}"))


def _fingerprint(title: str, lang: str, shelf_id: str | None) -> str:
    """What may change of a book the device holds: its title, its language, the shelf that files
    it. A change here is a `ChangedProductMetadata`; a new file is L17b's to announce."""
    return hashlib.sha256(f"{title}\x00{lang}\x00{shelf_id}".encode()).hexdigest()[:16]


@dataclass(frozen=True)
class Cursor:
    """Where a sync stopped: the last Edition consumed (`after`, None when the sync completed)
    and the instant of the first page the token belongs to (`version`)."""

    after: str | None
    version: int | None


def _encode(cursor: Cursor) -> str:
    raw = json.dumps({"a": cursor.after, "v": cursor.version}, separators=(",", ":"))
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii").rstrip("=")


def _decode(token: object) -> Cursor | None:
    """A continuation token of ours, or None: anything else (garbage, a token of another server)
    starts a full sync again — the device is never stuck with an answer it cannot read."""
    if not isinstance(token, str) or not token or len(token) > _MAX_TOKEN:
        return None
    try:
        raw = json.loads(
            base64.b64decode(token + "=" * (-len(token) % 4), altchars=b"-_", validate=True)
        )
    except (binascii.Error, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    after, version = raw.get("a"), raw.get("v")
    if after is not None and (not isinstance(after, str) or len(after) > 64):
        return None
    if version is not None and (not isinstance(version, int) or isinstance(version, bool)):
        return None
    return Cursor(after, version)


@dataclass(frozen=True)
class SyncPage:
    """One page of a sync: the body of the answer, the next continuation token, and whether
    another page follows (`X-Kobo-sync: continue`)."""

    body: dict[str, Any]
    continuation: str
    continue_sync: bool


@dataclass(frozen=True)
class _Book:
    """One Edition as the sync sees it: what the device is told, what it is filed on, and whether
    the device holds it (`kind`: "new" it does not, "changed" its metadata moved)."""

    edition_id: str
    title: str
    lang: str
    shelf_id: str | None
    shelf_name: str | None
    created_at: datetime
    kind: str = "new"

    @property
    def entitlement_id(self) -> str:
        return entitlement_of(self.edition_id)

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.title, self.lang, self.shelf_id)


@dataclass(frozen=True)
class _Point:
    """The state a device holds, as its last completed sync wrote it."""

    synced_at: datetime
    books: dict[str, dict[str, str]]  # entitlement id -> edition, title, fingerprint
    tags: dict[str, dict[str, Any]]  # tag id -> name, items


@dataclass(frozen=True)
class _Flight:
    """The sync now in flight, frozen at its first page and dated of that instant: what the
    pages serve from, and what the completion writes the point from. The catalogue changes
    under a sync; the answer of one sync does not."""

    t0: datetime
    kind: str  # "full": everything as new; "delta": the difference against the point
    walk: list[_Book]  # the books to serve, in identifier order, each marked new or changed
    served: list[str]  # the Editions the device was given so far (served, never the skipped)
    gone: list[str]  # the entitlements the device held when the sync started: a delta freezes
    # the ones the profile may no longer see, a full enumeration re-checks them all at the last
    # page and takes back what it may no longer see
    tags: dict[str, dict[str, Any]]  # "new", "changed", "deleted": tag id -> name, items
    states: list[dict[str, Any]]  # the reading positions, as the first page read them

    @property
    def version(self) -> int:
        return int(self.t0.timestamp())


def _book_document(book: _Book) -> dict[str, Any]:
    return {
        "edition": book.edition_id,
        "title": book.title,
        "lang": book.lang,
        "shelf": book.shelf_id,
        "created": book.created_at.isoformat(),
        "kind": book.kind,
    }


def _book_of(document: Any) -> _Book | None:
    if not isinstance(document, dict):
        return None
    edition, title = document.get("edition"), document.get("title")
    lang, shelf = document.get("lang"), document.get("shelf")
    created, kind = document.get("created"), document.get("kind")
    if not (
        isinstance(edition, str)
        and edition
        and isinstance(title, str)
        and title
        and isinstance(lang, str)
        and lang
        and isinstance(created, str)
        and kind in ("new", "changed")
    ):
        return None
    if shelf is not None and not isinstance(shelf, str):
        return None
    try:
        at = datetime.fromisoformat(created)
    except ValueError:
        return None
    return _Book(edition, title, lang, shelf, None, at, kind)


def _str_list(value: Any) -> list[str] | None:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        return None
    return list(value)


def _tags_of_document(value: Any) -> dict[str, dict[str, Any]] | None:
    if not isinstance(value, dict):
        return None
    for tag in value.values():
        if not isinstance(tag, dict) or not isinstance(tag.get("name"), str):
            return None
        if _str_list(tag.get("items")) is None:
            return None
    return dict(value)


def _flight_document(flight: _Flight) -> dict[str, Any]:
    return {
        "t0": flight.t0.isoformat(),
        "kind": flight.kind,
        "walk": [_book_document(book) for book in flight.walk],
        "served": flight.served,
        "gone": flight.gone,
        "tags": flight.tags,
        "states": flight.states,
    }


def _flight_of_document(document: Any) -> _Flight | None:
    """The flight a first page froze, or None: a document we cannot read (nobody's, another
    server's, a shape we no longer write) starts a full sync again — the device is never
    stuck with an answer it cannot read."""
    if not isinstance(document, dict) or document.get("kind") not in ("full", "delta"):
        return None
    t0 = document.get("t0")
    if not isinstance(t0, str):
        return None
    try:
        at = datetime.fromisoformat(t0)
    except ValueError:
        return None
    walk_raw = document.get("walk")
    if not isinstance(walk_raw, list):
        return None
    walk: list[_Book] = []
    for entry in walk_raw:
        book = _book_of(entry)
        if book is None:
            return None
        walk.append(book)
    served, gone = _str_list(document.get("served")), _str_list(document.get("gone"))
    if served is None or gone is None:
        return None
    tags_raw = document.get("tags")
    if not isinstance(tags_raw, dict):
        return None
    tags: dict[str, dict[str, Any]] = {}
    for name in ("new", "changed", "deleted"):
        part = _tags_of_document(tags_raw.get(name))
        if part is None:
            return None
        tags[name] = part
    states = document.get("states")
    if not isinstance(states, list) or not all(isinstance(state, dict) for state in states):
        return None
    return _Flight(at, str(document["kind"]), walk, served, gone, tags, list(states))


class KoboSync:
    def __init__(self, engine: Engine, now: Callable[[], datetime]) -> None:
        self._engine = engine
        self._now = now

    # -- what the device asks -------------------------------------------------------------------

    def initialization(self, *, principal: Principal, base: str) -> dict[str, Any]:
        """The resources of this server and of no other: the three routes of the sync, at the
        address the device already holds. The store is not among them (the proxy is off)."""
        return {
            "Resources": [
                {"Id": "initialization", "Href": f"{base}/v1/initialization"},
                {"Id": "auth_device", "Href": f"{base}/v1/auth/device"},
                {"Id": "library_sync", "Href": f"{base}/v1/library/sync"},
            ],
            "User": {"UserId": principal.user_id, "DisplayName": principal.username},
        }

    def sync(
        self,
        *,
        principal: Principal,
        token_id: str | None,
        base: str,
        continuation: object,
    ) -> SyncPage:
        """One page of the sync of this device. The first page of a sync freezes what its pages
        serve from; the scope is re-verified at each page; the sync point is written only when a
        sync completes, and holds what the device was given."""
        cursor = _decode(continuation)
        with self._engine.connect() as connection:
            device_id = self._device_of(connection, token_id)
            scope = reader_scope(connection, principal.user_id)
            if device_id is None:
                # nobody's token (the door vouches for a device): pages without a state
                after = cursor.after if cursor is not None and cursor.version is None else None
                return self._stateless_page(
                    connection, principal=principal, scope=scope, base=base, after=after
                )
            point = self._point_of(connection, device_id)
            if cursor is not None and cursor.after is not None:
                # a page of the sync in flight: the walk its first page froze
                flight = self._flight_of(connection, device_id)
                if flight is not None and cursor.version == flight.version:
                    return self._page(
                        connection,
                        scope=scope,
                        base=base,
                        device_id=device_id,
                        flight=flight,
                        after=cursor.after,
                    )
                # the flight is gone, or not the one the token names: the device starts over
                return self._begin_full(
                    connection,
                    principal=principal,
                    scope=scope,
                    base=base,
                    device_id=device_id,
                    point=point,
                )
            if (
                point is not None
                and cursor is not None
                and cursor.version == int(point.synced_at.timestamp())
            ):
                return self._begin_delta(
                    connection,
                    principal=principal,
                    scope=scope,
                    base=base,
                    device_id=device_id,
                    point=point,
                )
            # a full enumeration: no token, or one whose sync point is gone
            return self._begin_full(
                connection,
                principal=principal,
                scope=scope,
                base=base,
                device_id=device_id,
                point=point,
            )

    # -- the first page of a sync: the snapshot is frozen here ------------------------------------

    def _begin_full(
        self,
        connection: Connection,
        *,
        principal: Principal,
        scope: ReaderScope,
        base: str,
        device_id: str,
        point: _Point | None,
    ) -> SyncPage:
        """The first page of a full enumeration: everything the profile may see, as new. The
        walk, the tags and the reading states are frozen here and dated of now — the pages serve
        from them, whatever the catalogue does on the way. What the device already holds is
        frozen too: a full sync started over an existing point (no token, an unreadable one, or
        the cursor of a flight another request replaced) takes back what it holds and the
        profile may no longer see, at the last page."""
        t0 = self._now()
        books = self._books_of(connection, scope)
        tags = self._tags_of(connection, scope, books)
        states = self._reading_states(
            connection, principal.user_id, [book.edition_id for book in books], since=None
        )
        flight = _Flight(
            t0=t0,
            kind="full",
            walk=books,
            served=[],
            gone=sorted(point.books) if point is not None else [],
            tags={"new": tags, "changed": {}, "deleted": {}},
            states=states,
        )
        self._save_flight(device_id, flight)
        return self._page(
            connection, scope=scope, base=base, device_id=device_id, flight=flight, after=None
        )

    def _begin_delta(
        self,
        connection: Connection,
        *,
        principal: Principal,
        scope: ReaderScope,
        base: str,
        device_id: str,
        point: _Point,
    ) -> SyncPage:
        """The first page of a delta: the difference between the point and the catalogue, frozen
        here and dated of now. What the point held and the profile may no longer see is frozen as
        gone; the states are read over everything the profile may see, since the point."""
        t0 = self._now()
        current = self._books_of(connection, scope)
        current_tags = self._tags_of(connection, scope, current)
        held = set(point.books)
        walk: list[_Book] = []
        for book in current:
            if book.entitlement_id not in held:
                walk.append(replace(book, kind="new"))
            elif point.books[book.entitlement_id].get("fp") != book.fingerprint:
                walk.append(replace(book, kind="changed"))
        gone = sorted(e for e in point.books if e not in {b.entitlement_id for b in current})
        new_tags = {t: doc for t, doc in current_tags.items() if t not in point.tags}
        changed_tags = {
            t: doc for t, doc in current_tags.items() if t in point.tags and point.tags[t] != doc
        }
        deleted_tags = {t: point.tags[t] for t in point.tags if t not in current_tags}
        states = self._reading_states(
            connection,
            principal.user_id,
            [book.edition_id for book in current],
            since=point.synced_at,
        )
        flight = _Flight(
            t0=t0,
            kind="delta",
            walk=walk,
            served=[],
            gone=gone,
            tags={"new": new_tags, "changed": changed_tags, "deleted": deleted_tags},
            states=states,
        )
        self._save_flight(device_id, flight)
        return self._page(
            connection, scope=scope, base=base, device_id=device_id, flight=flight, after=None
        )

    # -- the pages of the sync in flight -----------------------------------------------------------

    def _page(
        self,
        connection: Connection,
        *,
        scope: ReaderScope,
        base: str,
        device_id: str,
        flight: _Flight,
        after: str | None,
    ) -> SyncPage:
        """One page of the sync in flight: the walk after the cursor, minus what the profile may
        no longer see — the scope is re-verified here, book by book. A book the profile may no
        longer see is consumed, not served: the cursor moves past it and no later page serves it."""
        candidates = [book for book in flight.walk if after is None or book.edition_id > after]
        visible = self._visible_ids_of(connection, scope)
        served_page: list[_Book] = []
        consumed, seen = after, 0
        for book in candidates:
            consumed, seen = book.edition_id, seen + 1
            if book.edition_id in visible:
                served_page.append(book)
                if len(served_page) == PAGE:
                    break
        more = seen < len(candidates)
        served_all = sorted(set(flight.served) | {book.edition_id for book in served_page})
        first_page = after is None
        delta = flight.kind == "delta"
        if more:
            deletions: list[str] = []
        elif delta:
            # what the first page froze as gone, and what was served and may no longer be seen
            deletions = sorted(
                set(flight.gone) | {entitlement_of(e) for e in served_all if e not in visible}
            )
        else:
            # a full enumeration takes back what the device holds and may no longer see: what it
            # held when the sync started and what it was served, minus what is still visible
            deletions = sorted(
                ({entitlement_of(e) for e in served_all} | set(flight.gone))
                - {entitlement_of(e) for e in visible}
            )
        result: dict[str, Any] = {
            "NewEntitlements": [
                self._entitlement(book, base) for book in served_page if book.kind == "new"
            ],
            "ChangedEntitlements": [],
            "DeletedEntitlements": deletions,
            "NewTagging": self._tagging(flight.tags["new"]) if first_page else [],
            "ChangedTagging": self._tagging(flight.tags["changed"]) if first_page and delta else [],
            "DeletedTagging": (
                self._deleted_tagging(flight.tags["deleted"]) if first_page and delta else []
            ),
            "ChangedProductMetadata": [
                {"Id": book.entitlement_id, "BookMetadata": self._metadata(book, base)}
                for book in served_page
                if book.kind == "changed"
            ],
            "ChangedReadingState": flight.states if first_page else [],
        }
        if more:
            self._save_flight(device_id, replace(flight, served=served_all))
            nxt = _encode(Cursor(consumed, flight.version))
            return SyncPage({"SyncStateResult": result, "ContinuationToken": nxt}, nxt, True)
        version = self._complete(
            device_id=device_id, flight=flight, served=served_all, visible=visible
        )
        done = _encode(Cursor(None, version))
        return SyncPage({"SyncStateResult": result, "ContinuationToken": done}, done, False)

    def _complete(
        self, *, device_id: str, flight: _Flight, served: list[str], visible: set[str]
    ) -> int:
        """The last page: the point is written from what the device was given, dated of the
        instant of the first page — whatever changed on the way is the next delta's to say. What
        it was served and may no longer be seen stays in the point (the device holds it: the next
        delta says it again); what it was never served is not in it (the next delta announces it).
        `visible` is the set the page read for its deletions, passed here and never read again:
        the deletions and the point are one reading of the scope, so a book hidden between two
        queries can never leave the point without being announced. The row is read again under
        the write lock, in the transaction that writes it: a point another request moved in
        between is never overwritten from a stale read, and the served books of a concurrent
        page of the same flight are merged in, never lost. Returns the version the completion
        token carries."""
        by_edition = {book.edition_id: book for book in flight.walk}
        with write_transaction(self._engine) as writer:
            row = writer.execute(
                sa.select(
                    reading.kobo_sync_point.c.books,
                    reading.kobo_sync_point.c.tags,
                    reading.kobo_sync_point.c.in_progress,
                ).where(reading.kobo_sync_point.c.device_id == device_id)
            ).first()
            in_place = _flight_of_document(row.in_progress) if row is not None else None
            if in_place is not None and in_place.version == flight.version:
                served = sorted(set(served) | set(in_place.served))
            books: dict[str, dict[str, str]] = {}
            tags: dict[str, dict[str, Any]] = {}
            old_books = row.books if row is not None and isinstance(row.books, dict) else {}
            old_tags = row.tags if row is not None and isinstance(row.tags, dict) else {}
            if flight.kind == "delta":
                gone = set(flight.gone)
                books = {e: doc for e, doc in old_books.items() if e not in gone}
                deleted = set(flight.tags["deleted"])
                tags = {t: doc for t, doc in old_tags.items() if t not in deleted}
            else:
                # what the device still holds and still sees stays in the point even when this
                # sync did not serve it again (hidden then re-shown on the way): the device was
                # told nothing about it, and the next delta must not forget it holds it
                held = set(old_books)
                books = {}
                for edition_id in visible:
                    entitlement = entitlement_of(edition_id)
                    if entitlement in held:
                        books[entitlement] = old_books[entitlement]
            for edition_id in served:
                book = by_edition.get(edition_id)
                if book is not None:
                    books[book.entitlement_id] = {
                        "edition": book.edition_id,
                        "title": book.title,
                        "fp": book.fingerprint,
                    }
            tags.update(flight.tags["new"])
            tags.update(flight.tags["changed"])
            writer.execute(
                sa.delete(reading.kobo_sync_point).where(
                    reading.kobo_sync_point.c.device_id == device_id
                )
            )
            writer.execute(
                sa.insert(reading.kobo_sync_point).values(
                    id=new_ulid(),
                    device_id=device_id,
                    synced_at=flight.t0,
                    books=books,
                    tags=tags,
                    in_progress={},
                )
            )
        return flight.version

    def _save_flight(self, device_id: str, flight: _Flight) -> None:
        """The flight, persisted at each page: a page the device asks again must find what it was
        given. The row is read again under the write lock, in the transaction that writes it, and
        two pages of one flight never lose each other's work: the served books are merged with
        what the lock finds, not written over it. The point of a delta stays as it is while the
        flight runs (the completion reads it under its own lock); a device with no row yet has an
        empty one, dated of the flight."""
        document = _flight_document(flight)
        with write_transaction(self._engine) as writer:
            row = writer.execute(
                sa.select(
                    reading.kobo_sync_point.c.id, reading.kobo_sync_point.c.in_progress
                ).where(reading.kobo_sync_point.c.device_id == device_id)
            ).first()
            if row is not None:
                in_place = _flight_of_document(row.in_progress)
                if in_place is not None and in_place.version == flight.version:
                    document["served"] = sorted(set(document["served"]) | set(in_place.served))
            if row is None:
                writer.execute(
                    sa.insert(reading.kobo_sync_point).values(
                        id=new_ulid(),
                        device_id=device_id,
                        synced_at=flight.t0,
                        books={},
                        tags={},
                        in_progress=document,
                    )
                )
            else:
                writer.execute(
                    sa.update(reading.kobo_sync_point)
                    .where(reading.kobo_sync_point.c.device_id == device_id)
                    .values(in_progress=document)
                )

    def _stateless_page(
        self,
        connection: Connection,
        *,
        principal: Principal,
        scope: ReaderScope,
        base: str,
        after: str | None,
    ) -> SyncPage:
        """A page for a token that is nobody's (the door vouches for a device; this is the
        defensive case): no flight and no point — the pages follow the cursor and nothing is
        written anywhere."""
        books = self._books_of(connection, scope, after=after, limit=PAGE + 1)
        more = len(books) > PAGE
        served = books[:PAGE]
        result: dict[str, Any] = {
            "NewEntitlements": [self._entitlement(book, base) for book in served],
            "ChangedEntitlements": [],
            "DeletedEntitlements": [],
            "NewTagging": (
                self._tagging(self._tags_of(connection, scope, self._books_of(connection, scope)))
                if after is None
                else []
            ),
            "ChangedTagging": [],
            "DeletedTagging": [],
            "ChangedProductMetadata": [],
            "ChangedReadingState": self._reading_states(
                connection, principal.user_id, [book.edition_id for book in served], since=None
            ),
        }
        if more:
            nxt = _encode(Cursor(books[PAGE - 1].edition_id, None))
            return SyncPage({"SyncStateResult": result, "ContinuationToken": nxt}, nxt, True)
        done = _encode(Cursor(None, int(self._now().timestamp())))
        return SyncPage({"SyncStateResult": result, "ContinuationToken": done}, done, False)

    # -- the pieces -------------------------------------------------------------------------------

    def _entitlement(self, book: _Book, base: str) -> dict[str, Any]:
        return {
            "Entitlement": {
                "EntitlementId": book.entitlement_id,
                "BookId": book.entitlement_id,
                "BookMetadata": self._metadata(book, base),
            }
        }

    def _metadata(self, book: _Book, base: str) -> dict[str, Any]:
        """What the device shows of a book. The download and the cover are the routes of this
        server under the device's own address (the bytes are L17b's; every other route answers
        the generic stub until then)."""
        return {
            "Title": book.title,
            "Language": book.lang,
            "DownloadUrls": [
                {
                    "DownloadUrl": f"{base}/v1/download/{book.entitlement_id}",
                    "Size": 0,
                    "Format": "KEPUB",
                }
            ],
            "CoverUrl": f"{base}/v1/covers/{book.entitlement_id}",
            "DateAdded": book.created_at.isoformat(),
        }

    def _tagging(self, tags: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "Tag": {"Id": tag_id, "Name": tag["name"], "Type": _TAG_TYPE},
                "ChangedTagging": [
                    {
                        "Tag": {"Id": tag_id, "Name": tag["name"], "Type": _TAG_TYPE},
                        "Items": tag["items"],
                        "IsDeleted": False,
                        "LastModified": self._now().isoformat(),
                    }
                ],
            }
            for tag_id, tag in tags.items()
        ]

    def _deleted_tagging(self, tags: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "Tag": {"Id": tag_id, "Name": tag["name"], "Type": _TAG_TYPE},
                "ChangedTagging": [
                    {
                        "Tag": {"Id": tag_id, "Name": tag["name"], "Type": _TAG_TYPE},
                        "Items": [],
                        "IsDeleted": True,
                        "LastModified": self._now().isoformat(),
                    }
                ],
            }
            for tag_id, tag in tags.items()
        ]

    def _books_of(
        self,
        connection: Connection,
        scope: ReaderScope,
        *,
        after: str | None = None,
        limit: int | None = None,
    ) -> list[_Book]:
        """The Editions this profile may see, in identifier order: the one filter of the catalogue
        is the condition of the query, so a page, a tag and the sync point can never disagree.
        An Edition on no shelf is visible to an unrestricted profile and belongs to no tag.

        The query walks `catalog.edition` alone: the condition speaks in subqueries of its own
        (a Work, a shelf), and a table joined here would be correlated into them. The title and
        the shelf name are read after, for the rows the query served."""
        edition = catalog.edition.c
        conditions = [visible_editions_clause(scope)]
        if after is not None:
            conditions.append(edition.id > after)
        query = (
            sa.select(
                edition.id, edition.work_id, edition.lang, edition.shelf_id, edition.created_at
            )
            .where(*conditions)
            .order_by(edition.id)
        )
        if limit is not None:
            query = query.limit(limit)
        rows = connection.execute(query).all()
        if not rows:
            return []
        work_ids = sorted({str(row.work_id) for row in rows})
        titles = dict(self._pairs_of(connection, catalog.work.c.id, catalog.work.c.title, work_ids))
        shelf_ids = sorted({str(row.shelf_id) for row in rows if row.shelf_id is not None})
        names = dict(self._pairs_of(connection, export.shelf.c.id, export.shelf.c.name, shelf_ids))
        return [
            _Book(
                str(row.id),
                titles[str(row.work_id)],
                str(row.lang),
                row.shelf_id,
                names.get(str(row.shelf_id)) if row.shelf_id else None,
                row.created_at,
            )
            for row in rows
        ]

    @staticmethod
    def _visible_ids_of(connection: Connection, scope: ReaderScope) -> set[str]:
        """The Editions the profile may see right now, identifiers only: the light re-verification
        of each page against the frozen walk. The query walks `catalog.edition` alone, for the
        condition speaks in subqueries of its own and a table joined here would be correlated
        into them."""
        rows = connection.execute(
            sa.select(catalog.edition.c.id).where(visible_editions_clause(scope))
        ).all()
        return {str(row.id) for row in rows}

    @staticmethod
    def _pairs_of(
        connection: Connection, key: Any, value: Any, wanted: list[str]
    ) -> list[tuple[str, str]]:
        """`key -> value` of the rows whose key is wanted, in chunks: a library holds more books
        than one `IN` may name."""
        found: list[tuple[str, str]] = []
        for start in range(0, len(wanted), _CHUNK):
            found.extend(
                connection.execute(
                    sa.select(key, value).where(key.in_(wanted[start : start + _CHUNK]))
                ).all()
            )
        return found

    def _tags_of(
        self, connection: Connection, scope: ReaderScope, books: list[_Book]
    ) -> dict[str, dict[str, Any]]:
        """The shelves this profile may be **named** with (`shelf_open_to`: the name of a shelf is
        household data) that are open to it (`allowed_profiles`) and hold at least one book it may
        see: a tag with nothing in it is not sent. The answer is the document the flight freezes
        and the point holds: tag id -> name, items."""
        shelf = export.shelf.c
        rows = connection.execute(
            sa.select(shelf.id, shelf.name, shelf.max_age_rating, shelf.allowed_profiles).order_by(
                shelf.name, shelf.id
            )
        ).all()
        items: dict[str, list[str]] = {}
        for book in books:
            if book.shelf_id is not None:
                items.setdefault(book.shelf_id, []).append(book.entitlement_id)
        tags: dict[str, dict[str, Any]] = {}
        for row in rows:
            allowed = row.allowed_profiles or []
            if not shelf_open_to(scope, row.max_age_rating):
                continue
            if allowed and scope.profile_id not in allowed:
                continue
            held = items.get(str(row.id))
            if held:
                tags[_tag_of(str(row.id))] = {"name": str(row.name), "items": sorted(held)}
        return tags

    def _reading_states(
        self,
        connection: Connection,
        user_id: str,
        edition_ids: list[str],
        *,
        since: datetime | None,
    ) -> list[dict[str, Any]]:
        """The reading positions of the profile on the served books, the latest of each: what the
        device is told it now reads. `since`: only those moved since the last sync (a full sync
        tells everything, the device held nothing)."""
        wanted = sorted(set(edition_ids))
        if not wanted:
            return []
        position, item = reading.reading_position.c, export.export_item.c
        rows = connection.execute(
            sa.select(item.edition_id, position.percent, position.server_ts)
            .select_from(
                reading.reading_position.join(
                    export.export_item,
                    sa.and_(
                        position.carrier_type == "export_item",
                        position.carrier_id == item.id,
                    ),
                )
            )
            .where(position.user_id == user_id, item.edition_id.in_(wanted))
            .order_by(position.server_ts, position.id)
        ).all()
        latest: dict[str, tuple[float, datetime]] = {}
        for row in rows:
            if row.percent is not None:
                latest[str(row.edition_id)] = (float(row.percent), row.server_ts)
        states = [
            (edition_id, percent, at)
            for edition_id, (percent, at) in latest.items()
            if since is None or at > since
        ]
        return [
            {
                "EntitlementId": entitlement_of(edition_id),
                "ReadingState": {
                    "Status": "InProgress",
                    "Percent": percent,
                    "LastModified": at.isoformat(),
                },
            }
            for edition_id, percent, at in sorted(states)
        ]

    def _device_of(self, connection: Connection, token_id: str | None) -> str | None:
        """The device a token belongs to (the sync point is the device's, not the token's: a
        rotation keeps it). None: the token is nobody's (the door checked it; a point is simply
        not written)."""
        if not isinstance(token_id, str) or not token_id:
            return None
        return connection.execute(
            sa.select(identity.device_token.c.device_id).where(
                identity.device_token.c.id == token_id
            )
        ).scalar()

    def _point_of(self, connection: Connection, device_id: str) -> _Point | None:
        row = connection.execute(
            sa.select(
                reading.kobo_sync_point.c.synced_at,
                reading.kobo_sync_point.c.books,
                reading.kobo_sync_point.c.tags,
            ).where(reading.kobo_sync_point.c.device_id == device_id)
        ).first()
        if row is None:
            return None
        books = row.books if isinstance(row.books, dict) else {}
        tags = row.tags if isinstance(row.tags, dict) else {}
        return _Point(row.synced_at, books, tags)

    def _flight_of(self, connection: Connection, device_id: str) -> _Flight | None:
        document = connection.execute(
            sa.select(reading.kobo_sync_point.c.in_progress).where(
                reading.kobo_sync_point.c.device_id == device_id
            )
        ).scalar()
        return None if not document else _flight_of_document(document)
