"""The Kobo sync service: what one device is given, and what changed since it was last seen
(M6 L17a, EF-INT-012, D20, EF-USR-006).

The protocol is not documented by the manufacturer: the surface is the minimal one the device
needs, written from the form its requests have. A book is an **Edition** (the thing a shelf
holds, a cover and a downloadable file belong to); the identifier the protocol speaks is derived
from it (`entitlement_of`: a UUID, stable, that says nothing of the database), and a tag is a
**shelf** the profile may be named with, holding the entitlements filed on it.

The scope is the profile of the device token, read from the database at each request
(`reader_scope`), and the one filter of the catalogue is a condition of every query: a page, a
tag and a count are made of what the profile may see, and nothing is filtered afterwards. The
scope is **re-verified at each page**: a ceiling lowered, a shelf closed or a book hidden between
two pages of one sync empties the next page, and the sync point written at the end holds only
what the last page could see.

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
from dataclasses import dataclass
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
    """Where a sync stopped: the last Edition served (`after`, None when the sync completed) and
    the sync point a delta is against (`version`, None for a full enumeration)."""

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
    """One Edition as the sync sees it: what the device is told, and what it is filed on."""

    edition_id: str
    title: str
    lang: str
    shelf_id: str | None
    shelf_name: str | None
    created_at: datetime

    @property
    def entitlement_id(self) -> str:
        return entitlement_of(self.edition_id)

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.title, self.lang, self.shelf_id)


@dataclass(frozen=True)
class _Tag:
    tag_id: str
    name: str
    items: list[str]


@dataclass(frozen=True)
class _Point:
    """The state a device holds, as its last completed sync wrote it."""

    synced_at: datetime
    books: dict[str, dict[str, str]]  # entitlement id -> edition, title, fingerprint
    tags: dict[str, dict[str, Any]]  # tag id -> name, items


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
        """One page of the sync of this device. The scope is read at this request and re-read at
        each page; the sync point is written only when a sync completes."""
        cursor = _decode(continuation)
        with self._engine.connect() as connection:
            device_id = self._device_of(connection, token_id)
            scope = reader_scope(connection, principal.user_id)
            point = self._point_of(connection, device_id)
            version = int(point.synced_at.timestamp()) if point is not None else None
            if (
                point is not None  # a delta needs a state to diff against
                and cursor is not None
                and cursor.version is not None
                and version == cursor.version
            ):
                return self._delta_page(
                    connection, principal=principal, scope=scope, point=point,
                    cursor=cursor, base=base, device_id=device_id,
                )  # fmt: skip
            # a full enumeration: no token, a page of one, or a delta whose sync point is gone
            after = cursor.after if cursor is not None and cursor.version is None else None
            return self._full_page(
                connection, principal=principal, scope=scope, base=base,
                device_id=device_id, after=after,
            )  # fmt: skip

    # -- a full enumeration: everything the profile may see, as new --------------------------------

    def _full_page(
        self,
        connection: Connection,
        *,
        principal: Principal,
        scope: ReaderScope,
        base: str,
        device_id: str | None,
        after: str | None,
    ) -> SyncPage:
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
                connection, principal.user_id, [b.edition_id for b in served], since=None
            ),
        }
        if more:
            next_after = books[PAGE - 1].edition_id
            nxt = _encode(Cursor(next_after, None))
            return SyncPage({"SyncStateResult": result, "ContinuationToken": nxt}, nxt, True)
        # the last page: the device now holds what the profile may see, as of now
        everything = self._books_of(connection, scope)
        version = self._write_point(
            device_id, everything, self._tags_of(connection, scope, everything)
        )
        done = _encode(Cursor(None, version))
        return SyncPage({"SyncStateResult": result, "ContinuationToken": done}, done, False)

    # -- a delta: what changed since the device was last seen --------------------------------------

    def _delta_page(
        self,
        connection: Connection,
        *,
        principal: Principal,
        scope: ReaderScope,
        point: _Point,
        cursor: Cursor,
        base: str,
        device_id: str | None,
    ) -> SyncPage:
        current = self._books_of(connection, scope)
        current_tags = self._tags_of(connection, scope, current)
        by_edition = {book.edition_id: book for book in current}
        held = set(point.books)
        arrived = frozenset(
            book.edition_id
            for book in current
            if book.entitlement_id not in held  # the device does not hold it
        )
        retitled = frozenset(
            book.edition_id
            for book in current
            if book.entitlement_id in held
            and point.books[book.entitlement_id].get("fp") != book.fingerprint
        )
        gone = sorted(e for e in point.books if e not in {b.entitlement_id for b in current})
        # the page serves the arrivals and the retitlings in one ordered walk, by cursor
        walk = sorted(arrived | retitled)
        after = cursor.after
        remaining = [e for e in walk if after is None or e > after]
        served, more = remaining[:PAGE], len(remaining) > PAGE
        first_page = after is None
        result: dict[str, Any] = {
            "NewEntitlements": [
                self._entitlement(by_edition[e], base) for e in served if e in arrived
            ],
            "ChangedEntitlements": [],
            "DeletedEntitlements": gone if first_page else [],
            "NewTagging": self._tagging(
                {t: current_tags[t] for t in current_tags if t not in point.tags}
            )
            if first_page
            else [],
            "ChangedTagging": self._tagging(
                {
                    t: current_tags[t]
                    for t in current_tags
                    if t in point.tags
                    and point.tags[t]
                    != {"name": current_tags[t].name, "items": current_tags[t].items}
                }
            )
            if first_page
            else [],
            "DeletedTagging": self._deleted_tagging(
                {t: point.tags[t] for t in point.tags if t not in current_tags}
            )
            if first_page
            else [],
            "ChangedProductMetadata": [
                {
                    "Id": by_edition[e].entitlement_id,
                    "BookMetadata": self._metadata(by_edition[e], base),
                }
                for e in served
                if e in retitled
            ],
            # a position moves on a book nothing else changed about: the states of a delta are
            # read over everything the profile may see, not over the walk of changed books
            "ChangedReadingState": (
                self._reading_states(
                    connection,
                    principal.user_id,
                    [b.edition_id for b in current],
                    since=point.synced_at,
                )
                if first_page
                else []
            ),
        }
        if more:
            nxt = _encode(Cursor(served[-1], cursor.version))
            return SyncPage({"SyncStateResult": result, "ContinuationToken": nxt}, nxt, True)
        version = self._write_point(device_id, current, current_tags)
        done = _encode(Cursor(None, version))
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

    def _tagging(self, tags: dict[str, _Tag]) -> list[dict[str, Any]]:
        return [
            {
                "Tag": {"Id": tag.tag_id, "Name": tag.name, "Type": _TAG_TYPE},
                "ChangedTagging": [
                    {
                        "Tag": {"Id": tag.tag_id, "Name": tag.name, "Type": _TAG_TYPE},
                        "Items": tag.items,
                        "IsDeleted": False,
                        "LastModified": self._now().isoformat(),
                    }
                ],
            }
            for tag in tags.values()
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
        )  # fmt: skip
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
                str(row.id), titles[str(row.work_id)], str(row.lang),
                row.shelf_id, names.get(str(row.shelf_id)) if row.shelf_id else None,
                row.created_at,
            )
            for row in rows
        ]  # fmt: skip

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
    ) -> dict[str, _Tag]:
        """The shelves this profile may be **named** with (`shelf_open_to`: the name of a shelf is
        household data) that are open to it (`allowed_profiles`) and hold at least one book it may
        see: a tag with nothing in it is not sent."""
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
        tags: dict[str, _Tag] = {}
        for row in rows:
            allowed = row.allowed_profiles or []
            if not shelf_open_to(scope, row.max_age_rating):
                continue
            if allowed and scope.profile_id not in allowed:
                continue
            held = items.get(str(row.id))
            if held:
                tags[_tag_of(str(row.id))] = _Tag(_tag_of(str(row.id)), str(row.name), sorted(held))
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

    def _point_of(self, connection: Connection, device_id: str | None) -> _Point | None:
        if device_id is None:
            return None
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

    def _write_point(self, device_id: str | None, books: list[_Book], tags: dict[str, _Tag]) -> int:
        """The state the device now holds, written at the completion of its sync: the version of
        the next delta. A device with no row (nobody's token) is not written."""
        now = self._now()
        if device_id is None:
            return int(now.timestamp())
        document = {
            book.entitlement_id: {
                "edition": book.edition_id,
                "title": book.title,
                "fp": book.fingerprint,
            }
            for book in books
        }
        shelves = {tag.tag_id: {"name": tag.name, "items": tag.items} for tag in tags.values()}
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.delete(reading.kobo_sync_point).where(
                    reading.kobo_sync_point.c.device_id == device_id
                )
            )
            connection.execute(
                sa.insert(reading.kobo_sync_point).values(
                    id=new_ulid(),
                    device_id=device_id,
                    synced_at=now,
                    books=document,
                    tags=shelves,
                )
            )
        return int(now.timestamp())
