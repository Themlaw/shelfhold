"""`backfill --gaps|--all` (EF-EXP-053): the estimate first, a confirmation above the thresholds.

Backfill is not a reserved action (it asks, it deletes nothing): what protects the disk and the
sources is the estimate (count, size, free space) and, above 100 Units or 5 GiB, a confirmation
that carries the figures the caller saw. A refusal for lack of confirmation is an answer with the
fresh estimate, not an exception, so that every door (route, socket, command) shows it the same way.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import sqlalchemy as sa

from shelfhold.acquisition.domain.backfill import BackfillMode
from shelfhold.acquisition.public import Backfill, BackfillConfirmationRequired, BackfillEstimate
from shelfhold.infrastructure.observability.disk_guard import disk_usage
from shelfhold.infrastructure.schema import acquisition
from shelfhold.ops.errors import InvalidChoiceError, UnknownSubjectError
from shelfhold.ops.reclaim import require_space

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["MODES", "BackfillOps"]

MODES = ("gaps", "all")


def _figures(estimate: BackfillEstimate) -> dict[str, Any]:
    return {
        "mode": estimate.mode, "units": estimate.units, "size_bytes": estimate.size_bytes,
        "free_bytes": estimate.free_bytes, "needs_confirmation": estimate.needs_confirmation,
    }  # fmt: skip


class BackfillOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def _free_bytes(self) -> int:
        for folder in (self._comp.settings.store_dir, self._comp.settings.data_dir):
            try:
                return disk_usage(folder).free
            except OSError:
                continue
        return 0

    def _service(self, edition_id: str, mode: str) -> tuple[Backfill, BackfillMode]:
        if mode not in MODES:
            raise InvalidChoiceError("choose `gaps` or `all`")
        follow = acquisition.series_follow
        with self._comp.engine.connect() as connection:
            followed = connection.execute(
                sa.select(follow.c.edition_id).where(follow.c.edition_id == edition_id)
            ).first()
        if followed is None:
            raise UnknownSubjectError("this edition is not followed")
        return Backfill(self._comp.engine, free_bytes=self._free_bytes), cast("BackfillMode", mode)

    def estimate(self, edition_id: str, mode: str) -> dict[str, Any]:
        service, checked = self._service(edition_id, mode)
        return _figures(service.estimate(edition_id, checked))

    def start(
        self, edition_id: str, mode: str, *, confirm_units: int | None, confirm_bytes: int | None
    ) -> dict[str, Any]:
        service, checked = self._service(edition_id, mode)
        require_space(self._comp, service.estimate(edition_id, checked).size_bytes)
        try:
            estimate = service.start(
                edition_id, checked, confirm_units=confirm_units, confirm_bytes=confirm_bytes
            )
        except BackfillConfirmationRequired as required:
            return {"started": False, **_figures(required.estimate)}
        return {"started": True, **_figures(estimate)}
