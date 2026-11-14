"""The type of a Kavita library, read again (EF-EXP-049).

The type of a library can change in Kavita (Manga to Comic): `plugin/parse` then reads the names
another way. The paths are frozen (I22) and Shelfhold never renames anything by itself, so a
type that no longer matches `kavita_library_type` pauses the shelf (`library_type_changed`,
exit `shelfhold target reconnect`), announces `target.library_type_changed` once, and replays
`parse-bulk` on a sample of the shelf with the new type to say what would be misread. Nothing is
written. The exit reads the type again: the shelf is released only once Kavita says again what
the shelf was set up for.

A library whose type cannot be read is not a changed library: it is left as it is.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.export.application.ports import TargetEvents
from shelfhold.export.application.target_state import (
    load_health,
    pause_shelf,
    resume_shelf,
    save_health,
)
from shelfhold.export.targets.kavita.adopt import library_type_named
from shelfhold.export.targets.kavita.client import KavitaClient
from shelfhold.export.targets.kavita.dto import LibraryType
from shelfhold.export.targets.kavita.errors import TargetError
from shelfhold.export.targets.kavita.revalidate import Revalidator
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import export

__all__ = [
    "LIBRARY_TYPE_CHANGED",
    "LibraryTypeChangeReport",
    "LibraryTypeService",
    "LibraryTypeStillChangedError",
    "resume_after_correction",
]

log = structlog.get_logger(__name__)

LIBRARY_TYPE_CHANGED = "library_type_changed"  # `shelf.paused_reason` and the waiting state


class LibraryTypeStillChangedError(TargetError):
    """The exit ran, and Kavita still gives a library another type than the shelf was set up for."""


@dataclass(frozen=True)
class LibraryTypeChangeReport:
    changed: tuple[str, ...] = ()  # the destinations whose library type is not the stored one
    paused_shelves: tuple[str, ...] = ()  # the shelves this run paused


@dataclass(frozen=True)
class _Linked:
    destination_id: str
    shelf_id: str
    remote_ref: str
    stored: LibraryType


def _linked(connection: Connection, target_id: str) -> list[_Linked]:
    """The destinations of a target with the library they are linked to, in a stable order."""
    dest, link, remote = (
        export.shelf_destination,
        export.shelf_destination_remote_library,
        export.remote_library,
    )
    rows = connection.execute(
        sa.select(dest.c.id, dest.c.shelf_id, dest.c.kavita_library_type, remote.c.remote_ref)
        .select_from(
            dest.join(link, link.c.shelf_destination_id == dest.c.id).join(
                remote, remote.c.id == link.c.remote_library_id
            )
        )
        .where(dest.c.target_id == target_id)
        .order_by(dest.c.id)
    )
    return [
        _Linked(r.id, r.shelf_id, r.remote_ref, library_type_named(r.kavita_library_type))
        for r in rows
    ]


async def _seen(client: KavitaClient, linked: list[_Linked]) -> dict[str, LibraryType]:
    """The type Kavita gives now, per library; a library it cannot read is not in the answer."""
    seen: dict[str, LibraryType] = {}
    for remote_ref in sorted({item.remote_ref for item in linked}):
        try:
            seen[remote_ref] = await client.library_type(int(remote_ref))
        except (TargetError, ValueError):
            log.info("target.library_type_unreadable", library=remote_ref)
    return seen


class LibraryTypeService:
    def __init__(self, engine: Engine, clock: Clock, events: TargetEvents) -> None:
        self._engine = engine
        self._clock = clock
        self._events = events

    async def check(self, client: KavitaClient, target_id: str) -> LibraryTypeChangeReport:
        """Read the type of every linked library; pause the shelf of each one that changed.

        A Kavita that does not answer raises `TargetError` before anything is touched.
        """
        await client.probe()
        with self._engine.connect() as connection:
            linked = _linked(connection, target_id)
        seen = await _seen(client, linked)
        changed = [i for i in linked if i.remote_ref in seen and seen[i.remote_ref] != i.stored]
        if not changed:
            return LibraryTypeChangeReport()
        announced: list[_Linked] = []
        paused: list[str] = []
        with write_transaction(self._engine) as connection:
            health = load_health(connection, target_id)
            already = health.library_type_changed
            for item in changed:
                held = connection.execute(
                    sa.select(export.shelf.c.paused_reason).where(
                        export.shelf.c.id == item.shelf_id
                    )
                ).scalar_one()
                # a pause for another reason stays: this one is picked up when it is lifted
                newly = held is None and pause_shelf(
                    connection, item.shelf_id, LIBRARY_TYPE_CHANGED
                )
                if newly:
                    paused.append(item.shelf_id)
                if newly or not already:
                    announced.append(item)
            health.library_type_changed = True
            health.refresh_waiting()
            save_health(connection, target_id, health)
        for item in announced:
            self._events.emit(
                "target.library_type_changed", target_id=target_id,
                destination_id=item.destination_id,
                stored_type=int(item.stored), seen_type=int(seen[item.remote_ref]),
            )  # fmt: skip
            await self._replay(client, target_id, item, seen[item.remote_ref])
        return LibraryTypeChangeReport(
            changed=tuple(i.destination_id for i in changed), paused_shelves=tuple(paused)
        )

    async def _replay(
        self, client: KavitaClient, target_id: str, item: _Linked, seen: LibraryType
    ) -> None:
        """What the new type would misread in this shelf: a report, the pause is already set."""
        try:
            await Revalidator(self._engine, self._clock, self._events).replay_sample(
                client, target_id, destination_id=item.destination_id, library_type=seen,
                kind="kavita_library_type",
            )  # fmt: skip
        except TargetError as error:  # the replay is informative, the pause does not wait for it
            log.warning("target.library_type_replay_failed", error=type(error).__name__)


async def resume_after_correction(engine: Engine, client: KavitaClient, target_id: str) -> None:
    """`target reconnect` for a changed library type: read the types again, release the shelves
    whose libraries are back to the stored type, and clear the wait once none differs.

    `client.health` is the document the exit saves afterwards. It raises
    `LibraryTypeStillChangedError` while a library still differs (the shelves that are back are
    released all the same), and `TargetError` if Kavita does not answer: the shelf stays paused.
    A pause that has another reason is not this exit's to lift.
    """
    await client.probe()
    with engine.connect() as connection:
        linked = _linked(connection, target_id)
    seen = await _seen(client, linked)
    differing = {i.shelf_id for i in linked if seen.get(i.remote_ref, i.stored) != i.stored}
    unread = {i.shelf_id for i in linked if i.remote_ref not in seen}
    with write_transaction(engine) as connection:
        for shelf_id in {i.shelf_id for i in linked} - differing - unread:
            resume_shelf(connection, shelf_id, LIBRARY_TYPE_CHANGED)
    if differing or unread:
        raise LibraryTypeStillChangedError(target_id)
    client.health.library_type_changed = False
    client.health.refresh_waiting()
