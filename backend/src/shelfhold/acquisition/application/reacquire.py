"""Whole-Unit reacquisition after a failed integrity check (EF-BIB-012).

A Unit the check marked `corrupt` is asked for again in full, from a live source, like any other
request (one open ticket per Unit, I52). Nothing is written over the damaged file and nothing is
deleted: the old archive stays where it is until a fresh copy has been verified, and the swap is
the same-path upgrade (`reason = integrity_reacquire`), which keeps every guard of its own and
tells the Activity that KOReader's position was reset (EF-EXP-016, D21).

A Unit with no live source (irreplaceable, EF-BIB-031) has nothing to be asked of: it is flagged
in the Activity (`ops.integrity_irreplaceable`, at most once a month) and left exactly as it is;
a good copy that turns up is not swapped in on its own, only the owner can do that (typed
identifier). The decision on what is irreplaceable is `library.irreplaceability`, which counts the
copies through the registry (`library.replica`), passed in as `irreplaceable`.
"""

from __future__ import annotations

import enum
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection, Engine

from shelfhold.acquisition.application.acquire import OPEN_STATES
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import acquisition, catalog, platform, storage

__all__ = ["ALERT_EVENT", "ReacquireOutcome", "Reacquirer", "Replacement"]

log = structlog.get_logger(__name__)

ALERT_EVENT: Final = "ops.integrity_irreplaceable"
COOLDOWN: Final = timedelta(days=7)  # a corrupt copy is not asked for again every night
ALERT_EVERY: Final = timedelta(days=30)
_GOOD: Final = frozenset({"upgraded", "up_to_date"})


class ReacquireOutcome(enum.Enum):
    REQUESTED = "requested"
    ALREADY_OPEN = "already_open"
    COOLING_DOWN = "cooling_down"
    IRREPLACEABLE = "irreplaceable"  # no live source: alerted, nothing else done
    NOT_CORRUPT = "not_corrupt"


@dataclass(frozen=True, slots=True)
class Replacement:
    # replaced | fresh_copy_ready | irreplaceable | no_fresh_copy | fresh_copy_corrupt
    # | not_upgraded | not_corrupt
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
        with self._engine.begin() as connection:
            if not self._is_corrupt(connection, unit_id):
                return ReacquireOutcome.NOT_CORRUPT
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
                        sa.or_(table.c.unit_id == unit_id, table.c.variant_id == variant[0]),
                        table.c.state.in_(OPEN_STATES),
                    )
                ).first():
                    return ReacquireOutcome.ALREADY_OPEN
                if connection.execute(
                    sa.select(table.c.id).where(
                        table.c.unit_id == unit_id, table.c.requested_at >= now - COOLDOWN
                    )
                ).first():
                    return ReacquireOutcome.COOLING_DOWN
                connection.execute(
                    sa.insert(table).values(
                        id=new_ulid(), target_kind="unit", variant_id=variant[0],
                        source_binding_id=variant[1], state="requested", requested_at=now,
                        unit_id=unit_id, visibility=None,  # an operation, for the admin
                    )
                )  # fmt: skip
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

    def complete(
        self,
        unit_id: str,
        *,
        verify: Callable[[str, str], bool],
        upgrade: Callable[[str, str], Sequence[str]],
    ) -> Replacement:
        """Once a fresh copy has arrived and been verified, the same-path upgrade swaps it in.

        `verify(unit_id, content_id)` reads the copy (the integrity check); `upgrade(unit_id,
        content_id)` is `SamePathUpgrade.upgrade_unit` with `integrity_reacquire`, its outcomes as
        words. An irreplaceable Unit is never swapped; the damaged file is never touched here."""
        content = storage.content
        with self._engine.connect() as connection:
            if not self._is_corrupt(connection, unit_id):
                return Replacement("not_corrupt")
            if self._live_variant(connection, unit_id) is None or self._irreplaceable(
                connection, unit_id
            ):
                return Replacement("irreplaceable")
            contents = [
                str(r[0])
                for r in connection.execute(
                    sa.select(content.c.id)
                    .where(content.c.unit_id == unit_id, content.c.format == "cbz")
                    .order_by(content.c.id.desc())
                )
            ]
        if len(contents) < 2:
            return Replacement("no_fresh_copy")  # only the damaged one: the ticket is still open
        fresh = next((c for c in contents if verify(unit_id, c)), None)
        if fresh is None:
            return Replacement("fresh_copy_corrupt")  # the old file stays; asked again later
        outcomes = list(upgrade(unit_id, fresh))
        if any(o not in _GOOD for o in outcomes):
            return Replacement("not_upgraded", fresh)
        with self._engine.begin() as connection:
            connection.execute(
                sa.update(catalog.unit)
                .where(catalog.unit.c.id == unit_id)
                .values(integrity_state="ok", integrity_checked_at=self._clock.now())
            )
        log.info("integrity.replaced", unit_id=unit_id, content_id=fresh)
        return Replacement("replaced" if outcomes else "fresh_copy_ready", fresh)
