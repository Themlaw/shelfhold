"""`backfill --gaps | --all` (EF-EXP-053): an estimate first, a confirmation above the thresholds.

Backfill is not a reserved action (L06 reserves what deletes, exports or exposes secrets): it
asks, it does not destroy. What protects the disk and the sources is the estimate (count, size,
free space) and, above 100 Units or 5 GiB, a confirmation that the caller must pass explicitly.
Starting does not request anything by itself: it sets the series' mode and the follow cycle brings
the Units back within its per-cycle caps, after the new chapters. The mode closes by itself.
"""

import json
from collections.abc import Callable
from typing import ClassVar

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.acquisition.application.planning import pending
from shelfhold.acquisition.domain.backfill import (
    CONFIRM_BYTES,
    CONFIRM_UNITS,
    DEFAULT_UNIT_BYTES,
    BackfillEstimate,
    BackfillMode,
    needs_confirmation,
)
from shelfhold.infrastructure.schema import acquisition, platform
from shelfhold.library.public import median_unit_bytes


class BackfillConfirmationRequired(Exception):
    code: ClassVar[str] = "backfill.confirmation_required"

    def __init__(self, estimate: BackfillEstimate) -> None:
        super().__init__(
            f"{self.code}: {estimate.units} Units, {estimate.size_bytes} bytes; confirm to go on"
        )
        self.estimate = estimate


def _key(edition_id: str) -> str:
    return f"backfill.confirmed.{edition_id}"


def estimate_with(
    connection: sa.Connection,
    edition_id: str,
    mode: BackfillMode,
    *,
    free_bytes: int,
    unit_bytes: int | None = None,
) -> BackfillEstimate:
    units = len(pending(connection, edition_id, mode))
    each = unit_bytes or median_unit_bytes(connection, edition_id) or DEFAULT_UNIT_BYTES
    size = units * each
    return BackfillEstimate(
        mode, units, size, free_bytes, needs_confirmation(units=units, size_bytes=size)
    )


def confirmed_ceiling(connection: sa.Connection, edition_id: str) -> tuple[int, int]:
    """What the user saw and accepted for this series (the thresholds when nothing above them)."""
    row = connection.execute(
        sa.select(platform.app_setting.c.value).where(
            platform.app_setting.c.key == _key(edition_id)
        )
    ).scalar()
    if row is None:
        return (CONFIRM_UNITS, CONFIRM_BYTES)
    data = json.loads(row)
    return (max(CONFIRM_UNITS, int(data["units"])), max(CONFIRM_BYTES, int(data["bytes"])))


def within_ceiling(estimate: BackfillEstimate, ceiling: tuple[int, int]) -> bool:
    return estimate.units <= ceiling[0] and estimate.size_bytes <= ceiling[1]


class Backfill:
    def __init__(
        self, engine: sa.Engine, *, free_bytes: Callable[[], int], unit_bytes: int | None = None
    ) -> None:
        self._engine = engine
        self._free_bytes = free_bytes
        self._unit_bytes = unit_bytes

    def estimate(self, edition_id: str, mode: BackfillMode) -> BackfillEstimate:
        with self._engine.connect() as connection:
            return estimate_with(
                connection, edition_id, mode,
                free_bytes=self._free_bytes(), unit_bytes=self._unit_bytes,
            )  # fmt: skip

    def start(
        self,
        edition_id: str,
        mode: BackfillMode,
        *,
        confirm_units: int | None = None,
        confirm_bytes: int | None = None,
    ) -> BackfillEstimate:
        """The confirmation is the figures the user saw, and they must equal the estimate taken
        now: if the series changed since (up or down), or the figures were made up, it is refused
        and the fresh estimate is shown again. The accepted ceiling is that estimate, kept on
        the series; the cycle stops the backfill if later
        arrivals would go beyond it."""
        estimate = self.estimate(edition_id, mode)
        if estimate.needs_confirmation and (
            confirm_units is None
            or confirm_bytes is None
            or estimate.units != confirm_units
            or estimate.size_bytes != confirm_bytes
        ):  # exactly what was shown: a larger figure would be a blank cheque
            raise BackfillConfirmationRequired(estimate)
        ceiling = (estimate.units, estimate.size_bytes)  # never the client's figures
        follow = acquisition.series_follow
        with self._engine.begin() as connection:
            changed = connection.execute(
                sa.update(follow)
                .where(follow.c.edition_id == edition_id)
                .values(backfill_mode=mode)
            ).rowcount
            if not changed:
                raise LookupError(f"edition {edition_id} is not followed")
            value = json.dumps({"units": ceiling[0], "bytes": ceiling[1]})
            connection.execute(
                sqlite_insert(platform.app_setting)
                .values(key=_key(edition_id), value=value)
                .on_conflict_do_update(index_elements=["key"], set_={"value": value})
            )
        return estimate
