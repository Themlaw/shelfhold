"""`backfill --gaps | --all` (EF-EXP-053): an estimate first, a confirmation above the thresholds.

Backfill is not a reserved action (L06 reserves what deletes, exports or exposes secrets): it
asks, it does not destroy. What protects the disk and the sources is the estimate (count, size,
free space) and, above 100 Units or 5 GiB, a confirmation that the caller must pass explicitly.
Starting does not request anything by itself: it sets the series' mode and the follow cycle brings
the Units back within its per-cycle caps, after the new chapters. The mode closes by itself.
"""

from collections.abc import Callable
from typing import ClassVar

import sqlalchemy as sa

from shelfhold.acquisition.application.planning import pending
from shelfhold.acquisition.domain.backfill import (
    DEFAULT_UNIT_BYTES,
    BackfillEstimate,
    BackfillMode,
    needs_confirmation,
)
from shelfhold.infrastructure.schema import acquisition
from shelfhold.library.public import median_unit_bytes


class BackfillConfirmationRequired(Exception):
    code: ClassVar[str] = "backfill.confirmation_required"

    def __init__(self, estimate: BackfillEstimate) -> None:
        super().__init__(
            f"{self.code}: {estimate.units} Units, {estimate.size_bytes} bytes; confirm to go on"
        )
        self.estimate = estimate


class Backfill:
    def __init__(
        self, engine: sa.Engine, *, free_bytes: Callable[[], int], unit_bytes: int | None = None
    ) -> None:
        self._engine = engine
        self._free_bytes = free_bytes
        self._unit_bytes = unit_bytes

    def estimate(self, edition_id: str, mode: BackfillMode) -> BackfillEstimate:
        with self._engine.connect() as connection:
            units = len(pending(connection, edition_id, mode))
            each = self._unit_bytes or median_unit_bytes(connection, edition_id)
        size = units * (each or DEFAULT_UNIT_BYTES)
        return BackfillEstimate(
            mode, units, size, self._free_bytes(), needs_confirmation(units=units, size_bytes=size)
        )

    def start(
        self, edition_id: str, mode: BackfillMode, *, confirmed: bool = False
    ) -> BackfillEstimate:
        estimate = self.estimate(edition_id, mode)
        if estimate.needs_confirmation and not confirmed:
            raise BackfillConfirmationRequired(estimate)
        follow = acquisition.series_follow
        with self._engine.begin() as connection:
            changed = connection.execute(
                sa.update(follow)
                .where(follow.c.edition_id == edition_id)
                .values(backfill_mode=mode)
            ).rowcount
        if not changed:
            raise LookupError(f"edition {edition_id} is not followed")
        return estimate
