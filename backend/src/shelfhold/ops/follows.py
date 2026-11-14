"""`follow` and `unfollow` (EF-SRC-029, EF-INT-014): one service for the host CLI and the routes.

Not reserved actions: following asks the connected service to keep a series, and unfollowing keeps
the series there unless `remove_upstream` is asked for, with a confirmation (it takes a library
entry out, never a file). The routes are the admin's, the CSRF of a session applies as everywhere.
Every refusal of the service or of the pause is a problem of the catalog, never a 500.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import sqlalchemy as sa

from shelfhold.acquisition.application.gap_watch import acknowledge, unacknowledge
from shelfhold.acquisition.public import (
    CadencePolicy,
    FollowConfirmationRequired,
    FollowIntent,
    FollowMode,
    FollowOutcome,
    FollowRefused,
    FollowRequests,
    SeriesPausedError,
    ServiceNotConnectedError,
    UnknownSeriesError,
    request_check_job,
)
from shelfhold.i18n import t
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.schema import acquisition, export
from shelfhold.ops.acquire import AcquireOps
from shelfhold.ops.errors import (
    CodedRefusal,
    ExitUnavailableError,
    InvalidChoiceError,
    UnknownSubjectError,
)
from shelfhold.ops.reclaim import estimate_units, require_space
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import SourceError

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["FollowOps"]


_NUMBER = re.compile(r"-?\d{1,9}\Z")
GAP_TRACKS = ("main", "annual", "special", "extra", "oneshot")


class FollowOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def gap_ack(
        self, edition_id: str, number: str, *, track: str, segment: str, undo: bool,
        actor_id: str | None,
    ) -> dict[str, Any]:  # fmt: skip
        """`follow gap-ack`: "this number does not exist" (EF-BIB-056). Only a gap the series
        shows can be acknowledged; `undo` takes the acknowledgement back."""
        if track not in GAP_TRACKS or not _NUMBER.fullmatch(number):
            raise InvalidChoiceError("a whole number, on one of the numbered tracks")
        number = str(int(number))
        gap = acquisition.series_gap
        with write_transaction(self._comp.engine) as connection:
            if undo:
                return {"changed": unacknowledge(
                    connection, edition_id, track=track, segment=segment, number=number
                ), "undone": True}  # fmt: skip
            ack = acquisition.gap_ack
            if connection.execute(
                sa.select(ack.c.edition_id).where(
                    ack.c.edition_id == edition_id, ack.c.track == track,
                    ack.c.number_segment == segment, ack.c.number_norm == number,
                )
            ).first():  # fmt: skip
                return {"changed": False, "undone": False}  # a replay changes nothing
            shown = connection.execute(
                sa.select(gap.c.edition_id).where(
                    gap.c.edition_id == edition_id, gap.c.track == track,
                    gap.c.number_segment == segment, gap.c.number_norm == number,
                )
            ).first()  # fmt: skip
            if shown is None:
                raise UnknownSubjectError("this series shows no such gap")
            changed = acknowledge(
                connection, edition_id, track=track, segment=segment, number=number,
                actor_id=actor_id, now=self._comp.clock.now(),
            )  # fmt: skip
        return {"changed": changed, "undone": False}

    def check(self, edition_id: str) -> dict[str, Any]:
        """`follow check`: look at this series at the next look, as a job of the news class
        (EF-SRC-038). The spacing by upstream source applies as for any look. Always-on: no
        source is asked, the job only reads what the service knows (`refresh` is false)."""
        pools = self._comp.pools()
        if pools is None:
            raise ExitUnavailableError("the job queue is not running")
        policy = CadencePolicy.from_settings(self._comp.settings)
        try:
            plugins = request_check_job(
                self._comp.engine, self._comp.clock, pools, edition_id, policy
            )
        except UnknownSeriesError:
            raise UnknownSubjectError("no followed series has this identifier") from None
        except SeriesPausedError:
            raise InvalidChoiceError("a series that is not paused") from None
        refresh = self._comp.check_mode() == "piloted"
        if not plugins:
            message = t("ops.follow_check_recent")
        elif refresh:
            message = t("ops.follow_check_queued")
        else:
            message = t("ops.follow_check_always_on")
        return {"queued": len(plugins), "refresh": refresh and bool(plugins), "message": message}

    def follow(
        self, source_key: str, remote_id: str, *, requester_user_id: str | None,
        visibility: str = "ops", units: int | None = None,
        mode: FollowMode | None = None, from_number: str | None = None,
        shelf_id: str | None = None, confirm_units: int | None = None,
        confirm_bytes: int | None = None,
    ) -> dict[str, Any]:  # fmt: skip
        """`mode` (the add sheet's choice, with `from_number` and `shelf_id`): the Units the follow
        brings and the shelf the Edition is filed on, applied when the series is discovered.
        Above the thresholds the confirmation must be the figures of the estimate taken now: if
        the series moved since the sheet counted, it is refused with the fresh estimate (409)."""
        intent = None
        if mode is not None:
            intent = FollowIntent(mode=mode, from_number=from_number, shelf_id=shelf_id)
            self._require_shelf(shelf_id)
            estimate = AcquireOps(self._comp).series_estimate(
                source_key, remote_id, mode, from_number
            )
            if estimate["needs_confirmation"] and (confirm_units, confirm_bytes) != (
                estimate["units"],
                estimate["size_bytes"],
            ):  # exactly what was shown: a larger figure would be a blank cheque
                raise CodedRefusal(
                    "backfill.confirmation_required",
                    "the figures confirmed are not those of the estimate",
                    extensions=estimate,
                )
            units = estimate["units"]
        # a followed series is a series that will be written
        require_space(self._comp, estimate_units(self._comp, units))
        ref = self._ref(source_key, remote_id)
        outcome = self._run(
            lambda requests: requests.follow(
                ref, requester_user_id=requester_user_id, visibility=visibility, intent=intent
            )
        )
        return {"state": outcome.state, "sent": outcome.sent}

    def _require_shelf(self, shelf_id: str | None) -> None:
        if shelf_id is None:
            return
        with self._comp.engine.connect() as connection:
            found = connection.execute(
                sa.select(export.shelf.c.id).where(export.shelf.c.id == shelf_id)
            ).first()
        if found is None:
            raise UnknownSubjectError("no shelf has this identifier")

    def unfollow(
        self, source_key: str, remote_id: str, *, remove_upstream: bool, confirm: bool
    ) -> dict[str, Any]:
        ref = self._ref(source_key, remote_id)
        outcome = self._run(
            lambda requests: requests.unfollow(
                ref, remove_upstream=remove_upstream, confirmed=confirm
            )
        )
        return {"state": outcome.state, "sent": outcome.sent, "removed": remove_upstream}

    @staticmethod
    def _ref(source_key: str, remote_id: str) -> SeriesRef:
        return SeriesRef(source_key=source_key, remote_id=remote_id, title="")

    def _run(self, work: Callable[[FollowRequests], Awaitable[FollowOutcome]]) -> FollowOutcome:
        # opened here: building the link runs a loop of its own, which cannot nest in `go`
        try:
            link = self._comp.suwayomi()
        except OSError as error:  # pragma: no cover (the guard refusing the address)
            raise ExitUnavailableError(str(error)) from None

        async def go() -> FollowOutcome:
            try:
                return await work(FollowRequests(self._comp.engine, self._comp.clock, lambda: link))
            finally:
                if link is not None:
                    await link.guarded.aclose()

        try:
            return self._comp.run(go())  # type: ignore[no-any-return]
        except ServiceNotConnectedError as error:
            raise ExitUnavailableError(str(error)) from None
        except (FollowRefused, FollowConfirmationRequired, SourceError) as error:
            raise CodedRefusal(error.code, str(error)) from None
