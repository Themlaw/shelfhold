"""The progress of each member, carried over the moves of a rename plan (EF-EXP-017, EF-EXP-030).

A rename, a reshelving or a binding migration inside one root makes Kavita scan again, and a scan
that no longer finds a series **deletes it with every reading of every account**. The progress a
member has is therefore read before the move and written back after it, with the member's own key
(EF-USR-012): never the dedicated account's, never another member's.

* `pull` (before a batch moves anything): `sync_member(mode="pull")`, one member after the other.
  What only Kavita knows is recorded in Shelfhold first. Nothing is written in Kavita.
* `restore` (after the batch and its scan): the series are found again by the folder they live in
  (`resolve_remote`: Kavita may have made new identifiers), the notes of the chapters that are gone
  are dropped, and each member's furthest **proven** page is written back
  (`sync_member(mode="restore")`: `reading_progress_proven`, never a reading made up, never a page
  behind what Kavita already holds).

Both halves are idempotent: a stop at any step is resumed by running the batch again. Kavita
scans asynchronously: a series it does not show yet is waited for (a few tries), then left to the
periodic pass, which carries a chapter it has never seen with the same rule.

The names of the members never leave through here: the results hold user identifiers and closed
codes, and a log line holds counts.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Collection, Coroutine, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Final, TypeVar

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine

from shelfhold.export.application.rename_plan import (
    CarryCoverage,
    CarryResult,
    ReaderCount,
    ReaderCounter,
)
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import ListingIncomplete, TargetError
from shelfhold.export.targets.kavita.readers import count_readers
from shelfhold.export.targets.kavita.series import (
    RemoteSeries,
    planned_collisions,
    resolve_remote,
)
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export, identity, reading
from shelfhold.infrastructure.secrets.keyring import Keyring
from shelfhold.reading.application.sync import (
    DEFAULT_MAX_SESSION_HOURS,
    ProgressRemote,
    RemoteUnavailable,
    sync_member,
)

log = structlog.get_logger(__name__)

__all__ = ["KavitaCarry"]

T = TypeVar("T")
TRIES: Final = 6
WAIT_S: Final = 5.0
_HOLDING: Final = ("written", "scanned", "resolved", "adopted")


class KavitaCarry:
    """`ProgressCarry` and `SeriesNames` of a Kavita target.

    `remote` is the progress adapter (structure with the dedicated key, readings with the members'
    own); `admin_client(target_id)` the client of the dedicated key (structure and names only)."""

    def __init__(
        self,
        engine: Engine,
        remote: ProgressRemote,
        clock: Any,
        *,
        admin_client: Callable[[str], KavitaClient],
        sleep: Callable[[float], None] = time.sleep,
        tries: int = TRIES,
        wait_s: float = WAIT_S,
        page_map_keyring: Callable[[], Keyring] | None = None,
        max_session_hours: float = DEFAULT_MAX_SESSION_HOURS,
    ) -> None:
        self._engine = engine
        self._remote = remote
        self._clock = clock
        self._admin_client = admin_client
        self._sleep = sleep
        self._tries = max(1, tries)
        self._wait_s = wait_s
        self._keyring = page_map_keyring
        self._max_session_hours = max_session_hours

    def __repr__(self) -> str:
        return "KavitaCarry()"

    # -- who is carried -----------------------------------------------------------------------

    def coverage(self, destination_id: str) -> CarryCoverage:
        destination, target = export.shelf_destination, export.export_target
        account, user = identity.destination_account, identity.user
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(destination.c.sync_progress, target.c.kind)
                .join(target, target.c.id == destination.c.target_id)
                .where(destination.c.id == destination_id)
            ).first()
            if row is None or not row.sync_progress or row.kind != "kavita":
                return CarryCoverage()  # progress is not synchronised here: nobody is carried
            live = connection.execute(
                sa.select(account.c.user_id, account.c.remote_user).where(
                    account.c.shelf_destination_id == destination_id,
                    account.c.revoked_at.is_(None),
                )
            ).all()
            held = {str(r.user_id) for r in live}
            declared = connection.execute(
                sa.select(user.c.id, user.c.username, user.c.kavita_username).where(
                    user.c.kavita_username.isnot(None)
                )
            ).all()
        return CarryCoverage(
            enabled=True,
            carried=tuple(sorted((str(r.user_id), str(r.remote_user)) for r in live)),
            unlinked=tuple(
                sorted(
                    (str(r.username), str(r.kavita_username))
                    for r in declared
                    if str(r.id) not in held
                )
            ),
        )

    def readers(self, destination_id: str) -> ReaderCounter:
        """The readers of Kavita in some folders of the destination, **the carried members'
        accounts apart** (they lose nothing), asked now (EF-EXP-037)."""

        def count(folders: Sequence[str]) -> ReaderCount:
            carried = [name for _user, name in self.coverage(destination_id).carried]
            try:
                target_id, library = self._where(destination_id)
                return self._in_thread(
                    target_id,
                    lambda client: count_readers(client, library, list(folders), carried=carried),
                )
            except (KeyError, ValueError, sa.exc.NoResultFound):
                return ReaderCount(0, exact=False)

        return count

    # -- the planning --------------------------------------------------------------------------

    def collision(self, destination_id: str, renames: Sequence[tuple[str, str]]) -> tuple[str, ...]:
        try:
            target_id, _library = self._where(destination_id)
            return self._in_thread(
                target_id,
                lambda client: planned_collisions(self._engine, client, destination_id, renames),
            )
        except (KeyError, ValueError, sa.exc.NoResultFound, TargetError):
            return ()

    # -- the two halves ------------------------------------------------------------------------

    def pull(self, destination_id: str, carriers: Collection[str]) -> CarryResult:
        members = self._members(destination_id)
        if not members:
            return CarryResult()
        try:
            chapters = [
                c for c in self._remote.chapters(destination_id) if c.carrier_id in set(carriers)
            ]
        except RemoteUnavailable:  # Kavita cannot be asked: nothing may move
            return CarryResult({user: "unavailable" for user in members})
        outcomes = {
            user: sync_member(
                self._engine, self._remote, user_id=user, destination_id=destination_id,
                clock=self._clock, chapters=chapters, mode="pull",
                page_map_keyring=self._keyring, max_session_hours=self._max_session_hours,
            )
            for user in members
        }  # fmt: skip
        return CarryResult(
            {user: o.code for user, o in outcomes.items()},
            pulled=sum(o.pulled for o in outcomes.values()),
        )

    def restore(self, destination_id: str, carriers: Collection[str]) -> CarryResult:
        members = self._members(destination_id)
        folders = self._folders(destination_id, carriers)
        missing = self._wait_for_series(destination_id, folders)
        if not members:
            return CarryResult(missing=missing)
        try:
            chapters = [
                c for c in self._remote.chapters(destination_id) if c.carrier_id in set(carriers)
            ]
        except RemoteUnavailable:
            return CarryResult({user: "unavailable" for user in members}, missing=missing)
        if {c.carrier_id for c in chapters} < set(carriers):
            # Kavita may not have indexed everything yet: what it shows is not kept, so that the
            # periodic pass asks again (and carries what is still missing, with the same rule)
            forget = getattr(self._remote, "forget", None)
            if callable(forget):
                forget(destination_id)
        self._drop_gone_notes(destination_id, carriers, {c.chapter_id for c in chapters})
        outcomes = {
            user: sync_member(
                self._engine, self._remote, user_id=user, destination_id=destination_id,
                clock=self._clock, chapters=chapters, mode="restore",
                page_map_keyring=self._keyring, max_session_hours=self._max_session_hours,
            )
            for user in members
        }  # fmt: skip
        return CarryResult(
            {user: o.code for user, o in outcomes.items()},
            pulled=sum(o.pulled for o in outcomes.values()),
            pushed=sum(o.pushed for o in outcomes.values()),
            missing=missing,
        )

    # -- the parts -----------------------------------------------------------------------------

    def _members(self, destination_id: str) -> list[str]:
        return [user for user, _name in self.coverage(destination_id).carried]

    def _folders(self, destination_id: str, carriers: Collection[str]) -> list[str]:
        """The series folders of the files, as the manifest says now."""
        record = export.export_record
        with self._engine.connect() as connection:
            rels = connection.execute(
                sa.select(record.c.rel_path).where(
                    record.c.shelf_destination_id == destination_id,
                    record.c.export_item_id.in_(sorted(carriers)),
                    record.c.rel_path.isnot(None),
                    record.c.state.in_(_HOLDING),
                )
            ).scalars()
            return sorted({str(rel).split("/", 1)[0] for rel in rels})

    def _wait_for_series(self, destination_id: str, folders: list[str]) -> tuple[str, ...]:
        """Resolve the series in Kavita; one it does not show yet is waited for, a few tries (a
        scan is asynchronous). Returns the folders still missing."""
        if not folders:
            return ()
        left = list(folders)
        for attempt in range(self._tries):
            wanted = tuple(left)

            def resolve(
                client: KavitaClient, wanted: tuple[str, ...] = wanted
            ) -> Coroutine[Any, Any, list[RemoteSeries]]:
                return resolve_remote(self._engine, client, destination_id, wanted)

            try:
                target_id, _library = self._where(destination_id)
                found: list[RemoteSeries] | None = self._in_thread(target_id, resolve)
            except (KeyError, ValueError, sa.exc.NoResultFound, TargetError, ListingIncomplete):
                found = None  # Kavita could not be asked: nothing is known, the wait goes on
            if found is not None:
                for series in found:
                    if series.renamed:
                        log.info("rename.series_renamed_in_kavita")  # never rewritten
                seen = {s.folder for s in found if s.kavita_id is not None}
                tracked = {s.folder for s in found}  # a folder with no series row: no wait
                left = [folder for folder in left if folder in tracked and folder not in seen]
            if not left or attempt == self._tries - 1:
                break
            self._sleep(self._wait_s)
        return tuple(left)

    def _drop_gone_notes(
        self, destination_id: str, carriers: Collection[str], present: Collection[int]
    ) -> None:
        """A note whose chapter Kavita no longer holds says nothing about the one that replaced
        it: it goes, so that the new chapter is seen as it is (a first look)."""
        note = reading.progress_sync_note
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.delete(note).where(
                    note.c.shelf_destination_id == destination_id,
                    note.c.carrier_id.in_(sorted(carriers)),
                    note.c.kavita_chapter_id.notin_(sorted(present)) if present else sa.true(),
                )
            )

    def _where(self, destination_id: str) -> tuple[str, int]:
        destination, link, library = (
            export.shelf_destination,
            export.shelf_destination_remote_library,
            export.remote_library,
        )
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(destination.c.target_id, library.c.remote_ref)
                .join(link, link.c.shelf_destination_id == destination.c.id)
                .join(library, library.c.id == link.c.remote_library_id)
                .where(destination.c.id == destination_id)
                .limit(1)
            ).one()
        return str(row.target_id), int(row.remote_ref)

    def _in_thread(
        self, target_id: str, work: Callable[[KavitaClient], Coroutine[Any, Any, T]]
    ) -> T:
        """Run a read against Kavita from synchronous code that may sit inside a loop: the client
        is made in a thread of its own, with no running loop (the fetcher runs one when built)."""

        def ask() -> T:
            client = self._admin_client(target_id)
            return asyncio.run(work(client))

        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(ask).result()
