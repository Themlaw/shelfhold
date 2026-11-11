"""Finding a series again in Kavita, whatever its current name (EF-EXP-057).

Kavita (0.9.1 and Kavita+) may rename a series. The name Shelfhold froze is therefore not an
identity: the series is looked up by the id it cached, then by the folder it lives in, and its
**current** name is read before anybody concludes to a collision (EF-EXP-032) or a match
(EF-EXP-038). A series renamed in the Kavita interface is the same series, not a second one, and
its name is not written over.
"""

from __future__ import annotations

import asyncio
import posixpath
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

import sqlalchemy as sa
import structlog
from sqlalchemy import Engine
from sqlalchemy.exc import IntegrityError

from shelfhold.export.application.field_guard import FieldGuard
from shelfhold.export.application.foreign_names import save_foreign_names
from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.series_age import SeriesAgeTask, due_ages, verified_age
from shelfhold.export.application.snapshot import load_context
from shelfhold.export.application.target_state import DestinationPausedError, FieldRegistry
from shelfhold.export.domain.field_owner import AGE_RATING, effective_age, should_push
from shelfhold.export.domain.kavita_access import age_of_rating, rating_for_age, reaches, tier
from shelfhold.export.domain.kavita_compat import blocks_family_shelf
from shelfhold.export.domain.naming import normalize_series_name
from shelfhold.export.domain.planner import (
    AGE_PAUSES,
    GATE_CLOSED,
    SERIES_AGE_STALE,
    SERIES_SPLIT,
    AgeGateFacts,
)
from shelfhold.export.targets.kavita import dto
from shelfhold.export.targets.kavita.audit import AccessAuditor
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.errors import ListingIncomplete, ProtocolError, TargetError
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import export
from shelfhold.sdk.age import AgeRating

__all__ = [
    "Resolution",
    "SeriesAgeGate",
    "SeriesAges",
    "SeriesResolver",
    "below_wanted",
    "blocking_age_reader",
    "folded",
    "refresh_foreign_names",
    "refresh_names",
]

log = structlog.get_logger(__name__)
READ_TIMEOUT_S = 60.0


def folded(name: str) -> str:
    return " ".join(name.casefold().split())


@dataclass(frozen=True)
class Resolution:
    series: dto.SeriesInfo | None
    by: str | None  # "cache" | "path" | None

    def renamed(self, frozen_name: str | None) -> bool:
        """Kavita shows another name than the one Shelfhold froze (never rewritten)."""
        return (
            self.series is not None
            and frozen_name is not None
            and folded(self.series.name) != folded(frozen_name)
        )


class SeriesResolver:
    def __init__(self, client: KavitaClient) -> None:
        self._client = client

    async def resolve(
        self, *, library_id: int, remote_folder: str, cached_id: int | None = None
    ) -> Resolution:
        if cached_id is not None:
            cached = (await self._client.series_by_ids([cached_id])).get(cached_id)
            if cached is not None and cached.library_id == library_id:
                return Resolution(cached, "cache")
        for series in await self._client.series_of_library(library_id):
            if series.lives_in(remote_folder):
                return Resolution(series, "path")
        return Resolution(None, None)

    async def collision(
        self, *, library_id: int, planned_name: str, remote_folder: str, own: Resolution
    ) -> dto.SeriesInfo | None:
        """The other series of the library the planned name would merge into, if any.

        The current names are read, not the cache: our own series, renamed, never collides with
        its former name, and a series that now bears the planned name does.
        """
        own_id = own.series.id if own.series else None
        for series in await self._client.series_of_library(library_id):
            if series.id == own_id or series.lives_in(remote_folder):
                continue
            if folded(series.name) == folded(planned_name):
                return series
        return None


async def refresh_names(engine: Engine, client: KavitaClient, destination_id: str) -> int:
    """Read the current names of the destination's series in Kavita and keep them in the cache
    (`export_series.name_raw` / `normalized_name`, EF-EXP-057).

    Kavita (and Kavita+) may rename a series: the planner would then see a collision with a name
    nobody bears any more, or miss one that somebody now does, and the pairing would compare
    titles with a stale one. The frozen name (`frozen_series`, the files') is never touched, and
    nothing is written in Kavita. A cached id Kavita no longer knows is found again by the folder
    the series lives in. Returns the number of names that changed.
    """
    series = export.export_series
    with engine.connect() as connection:
        ctx = load_context(connection, destination_id)
        rows = connection.execute(
            sa.select(
                series.c.id,
                series.c.kavita_series_id,
                series.c.series_dir,
                series.c.name_raw,
                series.c.normalized_name,
            ).where(
                series.c.shelf_destination_id == destination_id,
                series.c.kavita_series_id.isnot(None),
            )
        ).all()
        library = connection.execute(
            sa.select(export.remote_library.c.remote_ref).where(
                export.remote_library.c.id == ctx.remote_library_id
            )
        ).scalar()
    if not rows or library is None or not str(library).isdigit():
        return 0
    current = await client.series_by_ids([int(r.kavita_series_id) for r in rows])
    root = ctx.remote_folder(ctx.root_local) if ctx.root_local is not None else None
    lost = [r for r in rows if int(r.kavita_series_id) not in current]
    if lost and root is not None:  # a reinstall renumbers: the folder is the other identity
        listed = await client.series_of_library(int(library))
        for row in lost:
            folder = posixpath.join(root, row.series_dir) if row.series_dir else None
            found = next((s for s in listed if folder and s.lives_in(folder)), None)
            if found is not None:
                current[int(row.kavita_series_id)] = found
    changed = 0
    for row in rows:
        now = current.get(int(row.kavita_series_id))
        if now is None or folded(now.name) == folded(row.name_raw):
            continue
        normalized = normalize_series_name(now.name)
        if not normalized:
            continue
        try:
            with engine.begin() as connection:
                connection.execute(
                    sa.update(series)
                    .where(series.c.id == row.id)
                    .values(name_raw=now.name, normalized_name=normalized)
                )
        except IntegrityError:  # another series of the library already bears it: left as it is
            continue
        changed += 1
    return changed


async def refresh_foreign_names(engine: Engine, client: KavitaClient, destination_id: str) -> int:
    """Read the names of every series the target library shows, and keep their normalized forms
    (`foreign_names`): a series nobody from Shelfhold wrote still bears its name (EF-EXP-032).
    A listing that cannot be read leaves the last one in place; nothing is written in Kavita."""
    with engine.connect() as connection:
        ctx = load_context(connection, destination_id)
        library = connection.execute(
            sa.select(export.remote_library.c.remote_ref).where(
                export.remote_library.c.id == ctx.remote_library_id
            )
        ).scalar()
    if library is None or not str(library).isdigit():
        return 0
    try:
        listed = await client.series_of_library(int(library))
    except (TargetError, ListingIncomplete):
        return 0
    names = {normalize_series_name(s.name) for s in listed}
    save_foreign_names(engine, destination_id, sorted(n for n in names if n))
    return len(names)


class SeriesAges:
    """Push the class of each series by the API, locked, for the fields Shelfhold owns (EF-EXP-040).

    Run by the export job once Kavita has indexed the series, and again by `target reconnect`
    (EF-EXP-039). For each series, `FieldGuard.decide` answers from the `field_owner` registry
    (EF-EXP-057): a lock Shelfhold does not hold is a third party's and is never rewritten; a lock
    it holds whose value Kavita changed is drift, announced and pushed again; a lock it holds with
    the value it last pushed is rewritten only if the class moved since. The registry row is
    written before the request (ownership is never lost to a crash) and put back if it fails.
    """

    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events

    async def push(
        self, client: KavitaClient, destination_id: str, *, find_by_folder: bool = False
    ) -> list[int]:
        """The Kavita series ids whose age was pushed. `find_by_folder` looks a series up by its
        folder when no id is cached (a reinstalled Kavita), at the price of listing the library."""
        with self._engine.connect() as connection:
            tasks = due_ages(connection, destination_id)
            library = connection.execute(
                sa.select(export.remote_library.c.remote_ref)
                .select_from(
                    export.remote_library.join(
                        export.shelf_destination_remote_library,
                        export.shelf_destination_remote_library.c.remote_library_id
                        == export.remote_library.c.id,
                    )
                )
                .where(
                    export.shelf_destination_remote_library.c.shelf_destination_id == destination_id
                )
            ).scalar()
        if not tasks:
            return []
        try:
            return await self._push_tasks(client, destination_id, tasks, library, find_by_folder)
        except (TargetError, ListingIncomplete, DestinationPausedError) as error:
            log.info("export.age_push_failed", destination_id=destination_id,
                     reason=type(error).__name__)  # fmt: skip
            return []

    async def push_or_raise(self, client: KavitaClient, destination_id: str) -> list[int]:
        """`push`, but a Kavita that fails says so: the age gate closes on it (M2 L09)."""
        with self._engine.connect() as connection:
            tasks = due_ages(connection, destination_id)
        return await self._push_tasks(client, destination_id, tasks, None, False) if tasks else []

    async def _push_tasks(
        self,
        client: KavitaClient,
        destination_id: str,
        tasks: list[SeriesAgeTask],
        library: object,
        find_by_folder: bool,
    ) -> list[int]:
        listed: list[dto.SeriesInfo] = []
        lookup = find_by_folder and library is not None and str(library).isdigit()
        if lookup and any(t.kavita_series_id is None for t in tasks):
            listed = list(await client.series_of_library(int(str(library))))
        return await self._push_all(client, destination_id, tasks, listed)

    async def _push_all(
        self,
        client: KavitaClient,
        destination_id: str,
        tasks: list[SeriesAgeTask],
        listed: list[dto.SeriesInfo],
    ) -> list[int]:
        pushed: list[int] = []
        for task in tasks:
            kavita_id = task.kavita_series_id
            if kavita_id is None:
                found = next((s for s in listed if s.lives_in(task.folder)), None)
                if found is None:
                    continue  # not indexed yet: the next run of the job pushes it
                kavita_id = found.id
            try:
                current = await client.series_age(kavita_id)
            except ProtocolError:
                continue  # Kavita does not know this series (any more)
            if await self._push_one(client, destination_id, task, kavita_id, current):
                pushed.append(kavita_id)
        return pushed

    async def _push_one(
        self,
        client: KavitaClient,
        destination_id: str,
        task: SeriesAgeTask,
        kavita_id: int,
        current: dto.SeriesAge,
    ) -> bool:
        series = task.export_series_id
        with self._engine.begin() as connection:
            registry = FieldRegistry(connection)
            owned = registry.get(destination_id, series, AGE_RATING)
            ours = owned is not None and owned.pushed
            # the age Kavita derived from the tags is read only while nobody set it
            tag = None if current.locked or ours else current.age_rating
            age = effective_age(task.wanted, tag)
            if age is None:
                return False
            wanted = rating_for_age(age.min_age, x=age.x)
            action = FieldGuard(registry, self._events).decide(
                destination_id, series, AGE_RATING,
                locked_in_kavita=current.locked, kavita_value=current.age_rating,
            )  # fmt: skip
            if not should_push(action, wanted=wanted, owned=owned):
                return False
            registry.record_push(destination_id, series, AGE_RATING, wanted, self._clock.now())
        try:
            await client.set_series_age(current, wanted)
        except (TargetError, DestinationPausedError):
            with self._engine.begin() as connection:
                FieldRegistry(connection).restore(destination_id, series, AGE_RATING, owned)
            raise
        return True


class SeriesAgeGate:
    """The gate of the EPUB series (EF-EXP-040, EF-EXP-033, M2 L09), read by the export job just
    before it plans.

    An EPUB carries no age (D1): until Kavita holds the age of its series, locked, Kavita shows
    it "unknown". So, before anything is planned: Kavita must answer on a release at the floor of
    ENF-COMP-001 (0.9.1); the accounts are read, and the lowest tier of a non-admin account that
    reaches the library with "include unknowns" on is kept (it would see a first EPUB before its
    age is pushed: no first EPUB above that tier goes, and `target.child_exposure` names the
    setting); the ages due are pushed (a reclassification is pushed here, before any new file of
    the series is exposed); then every EPUB series is read back, on every Kavita series its
    files are in. A series is `verified` when each of them shows the lock Shelfhold holds
    (`field_owner`) at the value wanted now. A series Kavita split (`series_split`) or whose age
    reads below the one wanted (`series_age_stale`) is paused, listed by `status`, and takes
    nothing until a later read-back verifies it. Kavita that does not answer, refuses a push or
    drops the lock keeps the gate closed: fail closed.
    """

    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events
        self._ages = SeriesAges(engine, clock, events)

    async def open(self, client: KavitaClient, destination_id: str) -> AgeGateFacts:
        try:
            await client.probe()
            if blocks_family_shelf(client.health.version):
                log.info("export.age_gate_closed", destination_id=destination_id,
                         reason="version_below_floor")  # fmt: skip
                return GATE_CLOSED
            tier = await self._unknowns_tier(client, destination_id)
            refused: Exception | None = None
            try:
                await self._ages.push_or_raise(client, destination_id)
            except (TargetError, ListingIncomplete, DestinationPausedError) as error:
                refused = error  # the read-back still says which series stay below their age
            verified = await self._verified(client, destination_id)
        except (TargetError, ListingIncomplete, DestinationPausedError) as error:
            log.info("export.age_gate_closed", destination_id=destination_id,
                     reason=type(error).__name__)  # fmt: skip
            return GATE_CLOSED
        if refused is not None:
            log.info("export.age_gate_closed", destination_id=destination_id,
                     reason=type(refused).__name__)  # fmt: skip
            return GATE_CLOSED
        return AgeGateFacts(open=True, verified=frozenset(verified), unknowns_tier=tier)

    async def _unknowns_tier(self, client: KavitaClient, destination_id: str) -> int | None:
        """The lowest tier of a restricted account that sees the "unknown" series of the library."""
        with self._engine.connect() as connection:
            library = _library_ref(connection, destination_id)
        if library is None:
            return None
        accounts = await AccessAuditor(self._engine, self._clock, self._events).accounts(client)
        seeing = [
            a for a in accounts
            if not a.is_admin and a.include_unknowns and a.age_restricted
            and reaches(a, library)
        ]  # fmt: skip
        if not seeing:
            return None
        lowest = min(tier(a) for a in seeing)
        self._events.emit(
            "target.child_exposure", destination_id=destination_id,
            accounts=sorted(a.username for a in seeing if tier(a) == lowest),
            cause="include_unknowns", setting="include_unknowns",
        )  # fmt: skip
        return lowest

    async def _verified(self, client: KavitaClient, destination_id: str) -> list[str]:
        with self._engine.connect() as connection:
            tasks = due_ages(connection, destination_id)
            books = _epub_series(connection, destination_id)
        found: list[str] = []
        for task in tasks:
            if task.export_series_id not in books:
                continue  # a CBZ carries its age in its ComicInfo
            if task.split:
                self._pause(destination_id, task.export_series_id, SERIES_SPLIT)
                continue
            if task.kavita_series_id is None:
                continue  # not indexed yet: not verified
            try:
                current = await client.series_age(task.kavita_series_id)
            except ProtocolError:
                continue
            with self._engine.connect() as connection:
                owned = FieldRegistry(connection).get(
                    destination_id, task.export_series_id, AGE_RATING
                )
            if verified_age(
                task.wanted, owned, locked=current.locked, kavita_value=current.age_rating
            ):
                self._pause(destination_id, task.export_series_id, None)
                found.append(task.export_series_id)
                continue
            log.info("export.age_unverified", export_series_id=task.export_series_id)
            if below_wanted(task.wanted, current.age_rating):
                self._pause(destination_id, task.export_series_id, SERIES_AGE_STALE)
        return found

    def _pause(self, destination_id: str, series_id: str, reason: str | None) -> None:
        """Set (or clear, with None) the age pause of a series; announce it when it starts."""
        series = export.export_series
        with self._engine.begin() as connection:
            now = connection.execute(
                sa.select(series.c.paused_reason).where(series.c.id == series_id)
            ).scalar()
            if reason is None and now not in AGE_PAUSES:
                return  # no age pause, or another one (a foreign writer) not ours to lift
            if reason is not None and now is not None:
                return  # already paused, by us or by another cause that keeps it closed too
            connection.execute(
                sa.update(series).where(series.c.id == series_id).values(paused_reason=reason)
            )
        if reason is not None:
            self._events.emit(
                "reconcile.drift", destination_id=destination_id, export_series_id=series_id,
                cause=reason, field=AGE_RATING,
            )  # fmt: skip


def below_wanted(wanted: AgeRating, kavita_value: int) -> bool:
    """Kavita shows an age below the class wanted now (or none)."""
    shown = age_of_rating(kavita_value)
    needed = age_of_rating(rating_for_age(wanted.min_age, x=wanted.x))
    return shown is None or needed is None or shown < needed


def _library_ref(connection: sa.Connection, destination_id: str) -> int | None:
    link, library = export.shelf_destination_remote_library, export.remote_library
    ref = connection.execute(
        sa.select(library.c.remote_ref)
        .select_from(library.join(link, link.c.remote_library_id == library.c.id))
        .where(link.c.shelf_destination_id == destination_id)
    ).scalar()
    return int(ref) if ref is not None and str(ref).isdigit() else None


def _epub_series(connection: sa.Connection, destination_id: str) -> frozenset[str]:
    """The export series of the destination that hold an EPUB."""
    record, item, link = export.export_record, export.export_item, export.edition_export_series
    return frozenset(
        connection.execute(
            sa.select(link.c.export_series_id)
            .select_from(
                record.join(item, item.c.id == record.c.export_item_id).join(
                    link, link.c.edition_id == item.c.edition_id
                )
            )
            .where(record.c.shelf_destination_id == destination_id, record.c.format == "epub")
            .distinct()
        ).scalars()
    )


def blocking_age_reader(
    make_client: Callable[[], KavitaClient],
) -> Callable[[int], tuple[int, bool]]:
    """`(age_rating, locked)` of a Kavita series, read now, for the last look of the pipeline
    before it writes an EPUB (it runs under the locks, outside any event loop of its own: the
    read gets a client and a loop of its own in a worker thread)."""

    def in_thread(series_id: int) -> tuple[int, bool]:
        client = make_client()  # built in the worker thread too: no event loop is running there
        current = asyncio.run(client.series_age(series_id))
        return current.age_rating, current.locked

    def read(series_id: int) -> tuple[int, bool]:
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(in_thread, series_id).result(timeout=READ_TIMEOUT_S)

    return read
