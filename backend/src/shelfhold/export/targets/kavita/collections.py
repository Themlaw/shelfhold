"""The collections and reading lists of Shelfhold, pushed to Kavita (EF-EXP-025, M4 L18b).

A collection (the household's) or a reading list (a profile's own, or the household's) is made in
Kavita under the **service account** (the dedicated administrator key of the target, held by the
client; no member's key is ever used) and then **promoted**, so that the accounts that may read its
series see it. What a Kavita account is shown of a promoted one is cut down by Kavita to the series
of the libraries it may read; Shelfhold makes sure there is nothing to cut:

- **a collection goes only between libraries open to the same profiles.** Its series must all be in
  libraries of the same audience (the set of `(ceiling, profiles)` of the shelves linked to them,
  as for the related series of L19). One that crosses two audiences is pushed nowhere, not even cut
  down: a name such as "..., volume 9" would tell a child of a title she may not see;
- **a profile's own list carries her name** (`<profile> — <name>`), stays under the service account
  and is promoted only when the admin said so (`collection.public_approved_at`) *and* every series
  of it is in a library open to that profile (her ceiling, `allowed_profiles` of every shelf). A
  private list is never public by itself;
- **what a person put there by hand is never withdrawn.** Only the series Shelfhold added (the note
  of `collection_remote`) are taken back, and an object that holds something else is left in place,
  unpromoted, not deleted. An object of the service account that Shelfhold did not make, with the
  title it wants, is left alone: nothing is pushed under that title;
- **the note is written before the call.** What is about to be added, and the title being asked for
  while the identifier is not known, are noted first: a process killed at any step leaves a note too
  long, never too short, and the next pass finds the object by its title and finishes or withdraws
  it. The note outlives the collection (no foreign key): a deleted collection is a withdrawal due;
- **what became forbidden is withdrawn at once, a pause notwithstanding**: the promotion goes first,
  then the series, then the object when it is empty. Adding a series, creating an object and
  promoting wait for the end of a pause. Audiences are read from the database every pass.

Nothing is asked of Kavita when the database says nothing changed: each object keeps the last
complete push (`done`) in its note.
"""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_state import (
    DestinationPausedError,
    target_removal_guard,
)
from shelfhold.export.targets.kavita import dto
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.related import Audience, audiences
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import catalog, export

__all__ = ["PENDING", "CollectionsSync", "profile_title"]

log = structlog.get_logger(__name__)
PENDING: Final = "pending"  # `remote_id` of an object asked for whose identifier is not noted yet
_NO_CEILING: Final = 99
_DASH: Final = " — "


def profile_title(username: str, name: str) -> str:
    """The title of a profile's own list in Kavita: it carries her name."""
    return f"{username}{_DASH}{name}"


@dataclass(frozen=True)
class _Want:
    """What Shelfhold says one collection is in Kavita."""

    title: str
    series: tuple[int, ...]  # Kavita series ids, in the order of the collection
    promote: bool

    def signature(self) -> dict[str, Any]:
        return {"title": self.title, "series": list(self.series), "promote": self.promote}


@dataclass(frozen=True)
class _Row:
    collection_id: str
    remote_id: str
    note: dict[str, Any]

    @property
    def kind(self) -> str:
        return str(self.note.get("kind") or "collection")

    @property
    def owned(self) -> list[int]:
        return [int(i) for i in self.note.get("owned", []) if isinstance(i, int)]

    @property
    def done(self) -> dict[str, Any] | None:
        done = self.note.get("done")
        return done if isinstance(done, dict) else None


@dataclass(frozen=True)
class _Task:
    collection_id: str
    kind: str
    want: _Want | None  # None: nothing of it may be in Kavita
    row: _Row | None
    title: str = ""  # the title it has, or would have, in Kavita


class CollectionsSync:
    # What this process has looked at: a collection with no note, and what Shelfhold said of it at
    # that moment (`(target, collection, signature)`). With no note Shelfhold does not know what
    # Kavita holds under its title (a restore left the table empty): it looks once, and takes the
    # promotion off whatever the service account holds there that is not allowed, never more.
    _looked: ClassVar[set[tuple[str, str, str]]] = set()

    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events

    def due(self, target_id: str) -> list[_Task]:
        """What has to change in Kavita, from the database alone (none: no call is made)."""
        with self._engine.connect() as connection:
            return self._tasks(connection, target_id)

    async def publish(self, client: KavitaClient, target_id: str) -> list[str]:
        """Bring the service account's collections and lists to what Shelfhold says; the ids of the
        collections that were written. Nothing is sent when the database says nothing changed."""
        tasks = self.due(target_id)
        if not tasks:
            return []
        removal_guard = target_removal_guard(self._engine, target_id)
        written: list[str] = []
        try:
            held = {
                "collection": {s.id: s for s in await client.own_collections()},
                "reading_list": {s.id: s for s in await client.own_reading_lists()},
            }
            known = await client.series_by_ids(
                sorted({i for t in tasks if t.want for i in t.want.series})
            )
        except TargetError as error:
            log.info("export.collections_failed", target_id=target_id, reason=type(error).__name__)
            return []
        for task in tasks:
            try:
                changed = await self._settle(client, target_id, task, held, known, removal_guard)
            except DestinationPausedError:
                continue  # an addition waits for the end of a pause; the next one is another's
            except TargetError as error:  # Kavita unreachable: the next pass starts again
                log.info("export.collections_failed", target_id=target_id,
                         reason=type(error).__name__)  # fmt: skip
                break
            if changed:
                written.append(task.collection_id)
        return written

    # -- one collection ------------------------------------------------------------------------

    async def _settle(
        self,
        client: KavitaClient,
        target_id: str,
        task: _Task,
        held: dict[str, dict[int, dto.Shelf]],
        known: dict[int, dto.SeriesInfo],
        removal_guard: Callable[[], None],
    ) -> bool:
        want = task.want
        if want is not None:  # a series Kavita has not indexed (or no longer has) is left out
            kept = tuple(i for i in want.series if i in known)
            want = _Want(want.title, kept, want.promote) if kept else None
        mine = held[task.kind]
        row = task.row
        wrote = False
        shelf = self._find(row, mine)
        if row is not None and row.remote_id != PENDING and shelf is None:
            # Kavita no longer has it (a person deleted it): nothing to withdraw, the note goes
            self._forget(target_id, task.collection_id)
            row = None
        if want is None:
            if row is None:
                return await self._look(client, target_id, task, None, mine, removal_guard)
            return await self._withdraw(client, target_id, task, row, shelf, removal_guard)
        if row is None or row.remote_id == PENDING:
            taken = any(s.title == want.title for s in mine.values())
            if row is None and taken:
                log.info("export.collection_title_taken", target_id=target_id)
                # not ours: never taken over, never emptied, nothing is pushed under that title;
                # only its promotion goes when Shelfhold would not promote it
                return await self._look(client, target_id, task, want, mine, removal_guard)
            created = await self._create(client, target_id, task, want, row, mine)
            if created is None:
                return True  # asked for, not found yet: the next pass finds it by its title
            row, shelf, wrote = created, created_shelf(mine, created), True
        if shelf is None:  # pragma: no cover (the row names an object the listing has)
            return wrote
        return (
            await self._reconcile(client, target_id, task, want, row, shelf, removal_guard) or wrote
        )

    async def _look(
        self,
        client: KavitaClient,
        target_id: str,
        task: _Task,
        want: _Want | None,
        mine: dict[int, dto.Shelf],
        removal_guard: Callable[[], None],
    ) -> bool:
        """An object of the service account under the title of a collection that has no note. It
        is not Shelfhold's to take or to empty (a person may have made it); but a promoted one that
        Shelfhold would not promote (a restore lost the note of what it had pushed) is taken out of
        sight, which is the write that protects."""
        self._looked.add(self._key(target_id, task.collection_id, task.want))
        wrote = False
        for shelf in [s for s in mine.values() if s.title == task.title and s.promoted]:
            if want is None or not want.promote:
                with client.guarded_as(removal_guard):
                    await self._promote(client, task.kind, shelf.id, False)
                mine[shelf.id] = dto.Shelf(shelf.id, shelf.title, False, shelf.owner)
                wrote = True
        return wrote

    @staticmethod
    def _find(row: _Row | None, mine: dict[int, dto.Shelf]) -> dto.Shelf | None:
        if row is None:
            return None
        if row.remote_id == PENDING:
            title = row.note.get("pending_title")
            return next((s for s in mine.values() if s.title == title), None)
        return mine.get(int(row.remote_id)) if row.remote_id.isdigit() else None

    async def _create(
        self,
        client: KavitaClient,
        target_id: str,
        task: _Task,
        want: _Want,
        row: _Row | None,
        mine: dict[int, dto.Shelf],
    ) -> _Row | None:
        """Make the object under the service account. The note first: the title and what is about
        to be added; then the call; then the identifier, found by its title."""
        owned = sorted(set(want.series) | set(row.owned if row is not None else ()))
        note = {"kind": task.kind, "pending_title": want.title, "owned": owned,
                "done": None}  # fmt: skip
        found = self._find(row, mine)
        if found is None:
            self._write_row(target_id, task.collection_id, PENDING, note)
            if task.kind == "collection":
                await client.collection_add(0, want.title, want.series)
                fresh = {s.id: s for s in await client.own_collections()}
            else:
                await client.reading_list_create(want.title)
                fresh = {s.id: s for s in await client.own_reading_lists()}
            found = next((s for s in fresh.values() if s.title == want.title), None)
            if found is None:
                return None
            mine.update(fresh)
        note = {**note, "pending_title": None}
        self._write_row(target_id, task.collection_id, str(found.id), note)
        return _Row(task.collection_id, str(found.id), note)

    async def _reconcile(
        self,
        client: KavitaClient,
        target_id: str,
        task: _Task,
        want: _Want,
        row: _Row,
        shelf: dto.Shelf,
        removal_guard: Callable[[], None],
    ) -> bool:
        """Make what the object holds what Shelfhold says: protective writes first (the promotion
        when it is no longer allowed, then the series that are no longer wanted), then the series
        to add, then the promotion."""
        current = await self._held(client, task.kind, shelf)
        owned = set(row.owned)
        wanted = list(want.series)
        wrote = False
        # a series that is neither wanted nor Shelfhold's (a person put it there): the audience
        # rule has not seen it, so the object is not promoted while it holds it
        promote = want.promote and not (set(current) - set(wanted) - owned)
        if shelf.promoted and not promote:
            with client.guarded_as(removal_guard):
                await self._promote(client, task.kind, shelf.id, False)
            wrote = True
        gone = sorted((owned & set(current)) - set(wanted))
        if gone:
            with client.guarded_as(removal_guard):
                await self._remove(client, task.kind, shelf, gone)
            wrote = True
        add = [i for i in wanted if i not in current]
        keep = (owned - set(gone)) | set(add)
        if add:
            # the note first: a death after the call leaves it too long, never too short
            self._record(target_id, task.collection_id, row, owned=sorted(owned | set(add)))
            await self._add(client, task.kind, shelf, add)
            wrote = True
        if promote and not shelf.promoted:
            await self._promote(client, task.kind, shelf.id, True)
            wrote = True
        self._record(target_id, task.collection_id, row, owned=sorted(keep), done=want.signature())
        return wrote

    async def _withdraw(
        self,
        client: KavitaClient,
        target_id: str,
        task: _Task,
        row: _Row,
        shelf: dto.Shelf | None,
        removal_guard: Callable[[], None],
    ) -> bool:
        """Nothing of this may be in Kavita: out of sight first, then what Shelfhold added, then
        the object itself if nothing else is in it (what a person put there stays)."""
        if shelf is None:  # asked for and never found: nothing is there
            self._forget(target_id, task.collection_id)
            return False
        row = _Row(row.collection_id, str(shelf.id), {**row.note, "pending_title": None})
        current = await self._held(client, task.kind, shelf)
        owned = set(row.owned)
        with client.guarded_as(removal_guard):
            if shelf.promoted:
                await self._promote(client, task.kind, shelf.id, False)
            gone = sorted(owned & set(current))
            if gone:
                await self._remove(client, task.kind, shelf, gone)
            if not set(current) - set(gone):
                await self._delete(client, task.kind, shelf.id)
                self._forget(target_id, task.collection_id)
                return True
        # something else is in it: it is left to the person, out of sight, and the link is broken:
        # it is never taken back, and its title stays taken
        self._forget(target_id, task.collection_id)
        return True

    async def withdraw_all(self, client: KavitaClient, target_id: str) -> list[str]:
        """Take back everything Shelfhold pushed to this target (a detachment): out of sight, then
        what it added, then the objects that nothing else is in. What a person put there stays."""
        with self._engine.connect() as connection:
            rows = self._rows(connection, target_id)
        if not rows:
            return []
        removal_guard = target_removal_guard(self._engine, target_id)
        held = {
            "collection": {s.id: s for s in await client.own_collections()},
            "reading_list": {s.id: s for s in await client.own_reading_lists()},
        }
        done: list[str] = []
        for collection_id, row in rows.items():
            task = _Task(collection_id, row.kind, None, row)
            shelf = self._find(row, held[row.kind])
            if await self._withdraw(client, target_id, task, row, shelf, removal_guard):
                done.append(collection_id)
        return done

    # -- the calls, per kind -------------------------------------------------------------------

    @staticmethod
    async def _held(client: KavitaClient, kind: str, shelf: dto.Shelf) -> list[int]:
        if kind == "collection":
            return await client.series_of_collection(shelf.id)
        return list(dict.fromkeys(s for _item, s in await client.reading_list_items(shelf.id)))

    @staticmethod
    async def _promote(client: KavitaClient, kind: str, ident: int, promoted: bool) -> None:
        if kind == "collection":
            await client.collection_promote(ident, promoted)
        else:
            await client.reading_list_promote(ident, promoted)

    @staticmethod
    async def _remove(client: KavitaClient, kind: str, shelf: dto.Shelf, series: list[int]) -> None:
        if kind == "collection":
            await client.collection_remove(shelf, series)
            return
        items = await client.reading_list_items(shelf.id)
        for item_id, series_id in items:
            if series_id in series:
                await client.reading_list_delete_item(shelf.id, item_id)

    @staticmethod
    async def _add(client: KavitaClient, kind: str, shelf: dto.Shelf, series: list[int]) -> None:
        if kind == "collection":
            await client.collection_add(shelf.id, shelf.title, series)
            return
        for series_id in series:  # in the order of the list
            await client.reading_list_add(shelf.id, series_id)

    @staticmethod
    async def _delete(client: KavitaClient, kind: str, ident: int) -> None:
        if kind == "collection":
            await client.collection_delete(ident)
        else:
            await client.reading_list_delete(ident)

    # -- the note ------------------------------------------------------------------------------

    def _write_row(
        self, target_id: str, collection_id: str, remote_id: str, note: dict[str, Any]
    ) -> None:
        table = catalog.collection_remote
        with write_transaction(self._engine) as connection:
            done = connection.execute(
                sa.update(table)
                .where(table.c.collection_id == collection_id, table.c.target_id == target_id)
                .values(remote_id=remote_id, note=note, synced_at=self._clock.now())
            )
            if done.rowcount == 0:
                connection.execute(
                    sa.insert(table).values(
                        collection_id=collection_id, target_id=target_id, remote_id=remote_id,
                        note=note, synced_at=self._clock.now(),
                    )
                )  # fmt: skip

    def _record(
        self,
        target_id: str,
        collection_id: str,
        row: _Row,
        *,
        owned: list[int],
        done: dict[str, Any] | bool | None = False,
    ) -> None:
        """The note after a step: what Shelfhold added (`owned`), and, once a push is complete,
        what it said (`done`)."""
        note = {**row.note, "owned": owned}
        if done is not False:
            note["done"] = done
        self._write_row(target_id, collection_id, row.remote_id, note)

    def _forget(self, target_id: str, collection_id: str) -> None:
        table = catalog.collection_remote
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.delete(table).where(
                    table.c.collection_id == collection_id, table.c.target_id == target_id
                )
            )

    # -- what is due ---------------------------------------------------------------------------

    def _tasks(self, connection: Connection, target_id: str) -> list[_Task]:
        by_library = audiences(connection, target_id)
        rows = self._rows(connection, target_id)
        collections = self._collections(connection)
        series_of = self._series_of_works(connection, target_id, collections)
        profiles = self._profiles(connection)
        tasks: list[_Task] = []
        for collection in collections.values():
            want = self._want(collection, series_of, by_library, profiles)
            row = rows.pop(collection.id, None)
            if self._is_due(target_id, collection.id, want, row):
                title = want.title if want else _title_of(collection, profiles)
                tasks.append(_Task(collection.id, collection.kind, want, row, title))
        for gone_id, row in rows.items():  # the collection is gone: what it left is withdrawn
            if self._is_due(target_id, gone_id, None, row):
                tasks.append(_Task(gone_id, row.kind, None, row))
        return tasks

    def _is_due(
        self, target_id: str, collection_id: str, want: _Want | None, row: _Row | None
    ) -> bool:
        if row is not None and row.remote_id == PENDING:
            return True
        if row is None:  # no note: look once for what Kavita holds under the title
            return self._key(target_id, collection_id, want) not in self._looked
        if want is None:
            return row.done is not None or bool(row.owned)
        return row.done != want.signature()

    @staticmethod
    def _key(target_id: str, collection_id: str, want: _Want | None) -> tuple[str, str, str]:
        return (target_id, collection_id, json.dumps(None if want is None else want.signature()))

    @staticmethod
    def _rows(connection: Connection, target_id: str) -> dict[str, _Row]:
        table = catalog.collection_remote.c
        return {
            str(r.collection_id): _Row(
                str(r.collection_id), str(r.remote_id), r.note if isinstance(r.note, dict) else {}
            )
            for r in connection.execute(
                sa.select(table.collection_id, table.remote_id, table.note).where(
                    table.target_id == target_id
                )
            )
        }

    @staticmethod
    def _collections(connection: Connection) -> dict[str, _Collection]:
        c, item = catalog.collection.c, catalog.collection_item.c
        found: dict[str, _Collection] = {}
        for row in connection.execute(
            sa.select(c.id, c.kind, c.name, c.owner_id, c.public_approved_at).order_by(c.id)
        ):
            found[str(row.id)] = _Collection(
                str(row.id), str(row.kind), str(row.name),
                None if row.owner_id is None else str(row.owner_id),
                row.public_approved_at is not None, [],
            )  # fmt: skip
        for place in connection.execute(
            sa.select(item.collection_id, item.work_id).order_by(item.collection_id, item.position)
        ):
            found[str(place.collection_id)].works.append(str(place.work_id))
        return found

    @staticmethod
    def _profiles(connection: Connection) -> dict[str, tuple[str, int | None]]:
        from shelfhold.infrastructure.schema import identity

        user = identity.user.c
        return {
            str(r.id): (
                str(r.username),
                None if r.max_age_rating is None else int(r.max_age_rating),
            )
            for r in connection.execute(sa.select(user.id, user.username, user.max_age_rating))
        }

    @staticmethod
    def _series_of_works(
        connection: Connection, target_id: str, collections: dict[str, _Collection]
    ) -> dict[str, list[tuple[int, str]]]:
        """The Kavita series (id, remote library) each Work of a collection is, in this target."""
        wanted = {w for c in collections.values() for w in c.works}
        if not wanted:
            return {}
        edition, link = catalog.edition.c, export.edition_export_series.c
        series, library = export.export_series.c, export.remote_library.c
        found: dict[str, list[tuple[int, str]]] = defaultdict(list)
        for row in connection.execute(
            sa.select(edition.work_id, series.kavita_series_id, link.remote_library_id)
            .select_from(
                catalog.edition.join(export.edition_export_series, link.edition_id == edition.id)
                .join(export.export_series, series.id == link.export_series_id)
                .join(export.remote_library, library.id == link.remote_library_id)
            )
            .where(library.export_target_id == target_id, series.kavita_series_id.is_not(None))
            .order_by(edition.work_id, series.kavita_series_id)
        ):
            if str(row.work_id) in wanted and str(row.kavita_series_id).isdigit():
                entry = (int(row.kavita_series_id), str(row.remote_library_id))
                if entry not in found[str(row.work_id)]:
                    found[str(row.work_id)].append(entry)
        return found

    @staticmethod
    def _want(
        collection: _Collection,
        series_of: dict[str, list[tuple[int, str]]],
        by_library: dict[str, Audience],
        profiles: dict[str, tuple[str, int | None]],
    ) -> _Want | None:
        placed: list[tuple[int, str]] = []
        for work in collection.works:
            for entry in series_of.get(work, ()):
                if entry[0] not in {p[0] for p in placed}:
                    placed.append(entry)
        if not placed:
            return None  # nothing of it is in Kavita: nothing is made empty
        ids = tuple(i for i, _ in placed)
        libraries = {library for _, library in placed}
        if collection.owner_id is None:
            audience = {by_library.get(library) for library in libraries}
            if None in audience or len(audience) != 1:
                return None  # libraries open to different profiles: never one collection
            return _Want(collection.name, ids, promote=True)
        profile = profiles.get(collection.owner_id)
        if profile is None or not collection.approved:
            return None  # a private list goes to Kavita only once the admin has said so
        username, ceiling = profile
        opened = all(
            _open_to(by_library.get(library), collection.owner_id, ceiling) for library in libraries
        )
        audience = {by_library.get(library) for library in libraries}
        one_audience = None not in audience and len(audience) == 1  # as the household's rule
        return _Want(profile_title(username, collection.name), ids, promote=opened and one_audience)


@dataclass(frozen=True)
class _Collection:
    id: str
    kind: str
    name: str
    owner_id: str | None
    approved: bool
    works: list[str]


def _title_of(collection: _Collection, profiles: dict[str, tuple[str, int | None]]) -> str:
    """The title a collection has in Kavita, or would have."""
    if collection.owner_id is None:
        return collection.name
    profile = profiles.get(collection.owner_id)
    return collection.name if profile is None else profile_title(profile[0], collection.name)


def _open_to(audience: Audience | None, profile_id: str, ceiling: int | None) -> bool:
    """A library is open to a profile when every shelf linked to it is: she is in its
    `allowed_profiles` (or it has none), and its ceiling is at or under hers (a shelf with no
    ceiling is open to an adult only)."""
    if not audience:
        return False
    for shelf_ceiling, profiles in audience:
        if profiles and profile_id not in profiles:
            return False
        if ceiling is not None and shelf_ceiling > ceiling:
            return False
    return True


def created_shelf(mine: dict[int, dto.Shelf], row: _Row) -> dto.Shelf | None:
    return mine.get(int(row.remote_id)) if row.remote_id.isdigit() else None
