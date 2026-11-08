"""`follow` and `unfollow` (EF-SRC-029, EF-INT-014): one service for the host CLI and the routes.

Not reserved actions: following asks the connected service to keep a series, and unfollowing keeps
the series there unless `remove_upstream` is asked for, with a confirmation (it takes a library
entry out, never a file). The routes are the admin's, the CSRF of a session applies as everywhere.
Every refusal of the service or of the pause is a problem of the catalog, never a 500.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from shelfhold.acquisition.public import (
    FollowConfirmationRequired,
    FollowOutcome,
    FollowRefused,
    FollowRequests,
    ServiceNotConnectedError,
)
from shelfhold.ops.errors import CodedRefusal, ExitUnavailableError
from shelfhold.sdk.refs import SeriesRef
from shelfhold.sdk.source import SourceError

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["FollowOps"]


class FollowOps:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp

    def follow(
        self, source_key: str, remote_id: str, *, requester_user_id: str | None
    ) -> dict[str, Any]:
        ref = self._ref(source_key, remote_id)
        outcome = self._run(
            lambda requests: requests.follow(
                ref, requester_user_id=requester_user_id, visibility="ops"
            )
        )
        return {"state": outcome.state, "sent": outcome.sent}

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
