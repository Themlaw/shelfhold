"""Whole-Unit reacquisition after a failed integrity check (EF-BIB-012).

A Unit the check marked `corrupt` is asked for again in full, from a live source, like any other
request (one open ticket per Unit, I52). The fresh bytes are put **in the place of the damaged
file** (`export.LibraryRepair`, called by the ticket: staged outside the tree a library scans,
swapped under the intent journal, the damaged file to the bin of its root, never deleted). The
damaged copy stops counting as an independent copy as soon as it is found. The Unit is `ok` once
every place that held a damaged copy holds an intact one, the served path read again.

A Unit with no live source (irreplaceable, EF-BIB-031) has nothing to be asked of: it is flagged
in the Activity (`ops.integrity_irreplaceable`, at most once a month) and left exactly as it is;
a good copy that turns up is not swapped in on its own, only the owner can do that (typed
identifier). The decision on what is irreplaceable is `library.irreplaceability`, which counts the
copies through the registry (`library.replica`), passed in as `irreplaceable`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.tickets import DownloadTickets
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition, catalog, platform
from shelfhold.library.public import fresh_repair_content

__all__ = ["ALERT_EVENT", "ReacquireOutcome", "Reacquirer", "Replacement"]

log = structlog.get_logger(__name__)

ALERT_EVENT: Final = "ops.integrity_irreplaceable"
COOLDOWN: Final = timedelta(days=7)  # a corrupt copy is not asked for again every night
ALERT_EVERY: Final = timedelta(days=30)


class ReacquireOutcome(enum.Enum):
    REQUESTED = "requested"
    ALREADY_OPEN = "already_open"
    COOLING_DOWN = "cooling_down"
    IRREPLACEABLE = "irreplaceable"  # no live source: alerted, nothing else done
    NOT_CORRUPT = "not_corrupt"


@dataclass(frozen=True, slots=True)
class Replacement:
    # replaced | irreplaceable | no_fresh_copy | fresh_copy_corrupt | not_corrupt
    outcome: str
    content_id: str | None = None


class Reacquirer:
    def __init__(
        self,
        engine: Engine,
        bus: EventBus,
        clock: Clock,
        *,
        irreplaceable: Callable[[Connection, str], bool],
    ) -> None:
        self._engine = engine
        self._bus = bus
        self._clock = clock
        self._irreplaceable = irreplaceable

    def corrupt_units(self, limit: int) -> list[str]:
        """The Units the check marked corrupt: a read of the database, no disk is woken."""
        unit = catalog.unit
        with self._engine.connect() as connection:
            return [
                str(r[0])
                for r in connection.execute(
                    sa.select(unit.c.id)
                    .where(unit.c.integrity_state == "corrupt")
                    .order_by(unit.c.integrity_checked_at, unit.c.id)
                    .limit(limit)
                )
            ]

    def _is_corrupt(self, connection: Connection, unit_id: str) -> bool:
        state = connection.execute(
            sa.select(catalog.unit.c.integrity_state).where(catalog.unit.c.id == unit_id)
        ).scalar_one_or_none()
        return state == "corrupt"

    def reacquire(self, unit_id: str) -> ReacquireOutcome:
        now = self._clock.now()
        announced = []
        with write_transaction(self._engine) as connection:
            flagged = connection.execute(
                sa.select(catalog.unit.c.integrity_checked_at).where(catalog.unit.c.id == unit_id)
            ).scalar()
            if not self._is_corrupt(connection, unit_id):
                return ReacquireOutcome.NOT_CORRUPT
            flagged = flagged or now - COOLDOWN
            variant = self._live_variant(connection, unit_id)
            if variant is None or self._irreplaceable(connection, unit_id):
                if self._alerted_recently(connection, unit_id):
                    return ReacquireOutcome.IRREPLACEABLE
                announced.append(self._bus.publish(connection, ALERT_EVENT, {"unit_id": unit_id}))
                outcome = ReacquireOutcome.IRREPLACEABLE
            else:
                table = acquisition.download_request
                if connection.execute(
                    sa.select(table.c.id).where(
                        table.c.unit_id == unit_id, table.c.state.in_(("requested", "downloaded"))
                    )
                ).first():
                    return ReacquireOutcome.ALREADY_OPEN
                if connection.execute(
                    sa.select(table.c.id).where(
                        table.c.unit_id == unit_id,
                        table.c.requested_at >= now - COOLDOWN,
                        table.c.requested_at >= flagged,  # the reception itself is not an attempt
                    )
                ).first():
                    return ReacquireOutcome.COOLING_DOWN
                if not DownloadTickets.open(  # one open ticket per Unit, its Unit row with it
                    connection,
                    binding_id=variant[1],
                    variant_id=variant[0],
                    unit_id=unit_id,
                    now=now,
                ):
                    return ReacquireOutcome.ALREADY_OPEN
                outcome = ReacquireOutcome.REQUESTED
        self._bus.deliver(announced)
        log.info("integrity.reacquire", unit_id=unit_id, outcome=outcome.value)
        return outcome

    @staticmethod
    def _live_variant(connection: Connection, unit_id: str) -> tuple[str, str] | None:
        variant, binding = catalog.unit_variant, acquisition.source_binding
        row = connection.execute(
            sa.select(variant.c.id, variant.c.source_binding_id)
            .join(binding, binding.c.id == variant.c.source_binding_id)
            .where(variant.c.unit_id == unit_id, variant.c.removed_at.is_(None), binding.c.active)
            .order_by(variant.c.preferred.desc(), binding.c.priority, variant.c.id)
            .limit(1)
        ).first()
        return None if row is None else (str(row[0]), str(row[1]))

    def _alerted_recently(self, connection: Connection, unit_id: str) -> bool:
        since = self._clock.now() - ALERT_EVERY
        return (
            connection.execute(
                sa.select(platform.event.c.id).where(
                    platform.event.c.type == ALERT_EVENT,
                    platform.event.c.occurred_at >= since,
                    sa.func.json_extract(platform.event.c.payload, "$.unit_id") == unit_id,
                )
            ).first()
            is not None
        )

    def complete(self, unit_id: str, *, repaired: Callable[[str], bool]) -> Replacement:
        """After a repair put fresh bytes in the place of a damaged library file
        (`export.LibraryRepair`, through the ticket).

        Nothing is read until a file was replaced since the last attempt. Then `repaired(unit)`
        reads every independent copy again, the served path included, `/library` and `/store`, and
        says whether each place that held a damaged copy now holds an intact one; only then is the
        Unit `ok`. An irreplaceable Unit is never touched; the damaged files are never deleted
        here (the repair moved them to the bin)."""
        with self._engine.connect() as connection:
            row = connection.execute(
                sa.select(catalog.unit.c.integrity_state, catalog.unit.c.integrity_checked_at)
                .where(catalog.unit.c.id == unit_id)
            ).one_or_none()  # fmt: skip
            if row is None or row.integrity_state != "corrupt":
                return Replacement("not_corrupt")
            if self._live_variant(connection, unit_id) is None or self._irreplaceable(
                connection, unit_id
            ):
                return Replacement("irreplaceable")
            content_id = fresh_repair_content(connection, unit_id, row.integrity_checked_at)
        if content_id is None:
            return Replacement("no_fresh_copy")  # the ticket is open, or nothing new came
        done = repaired(unit_id)
        # the attempt is dated: no new copy, no new read
        with write_transaction(self._engine) as connection:
            connection.execute(
                sa.update(catalog.unit)
                .where(catalog.unit.c.id == unit_id)
                .values(
                    integrity_state="ok" if done else "corrupt",
                    integrity_checked_at=self._clock.now(),
                )
            )
        if not done:
            return Replacement("fresh_copy_corrupt", content_id)
        log.info("integrity.repaired", unit_id=unit_id, content_id=content_id)
        return Replacement("replaced", content_id)
