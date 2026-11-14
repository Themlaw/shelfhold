"""Follow and unfollow a series of the connected service from Shelfhold (EF-SRC-029, EF-INT-014).

An approved request makes **exactly one** entry in the service's library, whatever happens around
it: a replay, a double click, two workers approving together. Three things hold that:

- the unique key `(source_key, remote_id)` of `follow_request`: there is one request per series;
- the claim, taken in a `BEGIN IMMEDIATE` transaction together with the look at the pause: of two
  workers, the second one waits for the first, then finds the request `following` (the first one
  is on its way: nothing is sent) or `followed` (nothing to do);
- the service's own `set_follow`, which attaches a series it already holds and never adds it twice,
  so even a crash between the call and its record is repaired by asking again.

A follow that does not run inside the cycle looks at the pause of the service first (`source
instance changed` and every other one): a Suwayomi installed again must not get a `Shelfhold`
category made in it before the admin said it is the one (EF-SRC-036). It also reads the
fingerprint, as the cycle does, which the adapter wants before it creates the category. A refused
follow creates nothing.

Unfollowing keeps the series in the service; it leaves it only when asked, and confirmed.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from shelfhold.acquisition.application.failures import FailureLedger, from_service
from shelfhold.acquisition.application.instance import check_instance
from shelfhold.acquisition.domain.follow_intent import FollowIntent
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.requesters import still_a_user
from shelfhold.infrastructure.schema import acquisition
from shelfhold.plugin_host.errors import ServiceInstanceChanged
from shelfhold.sdk.refs import SeriesRef

__all__ = [
    "CLAIM_TTL",
    "FollowConfirmationRequired",
    "FollowOutcome",
    "FollowRefused",
    "FollowRequests",
    "ServiceLink",
    "ServiceNotConnectedError",
]

_REQUEST = acquisition.follow_request
CLAIM_TTL = timedelta(minutes=2)  # a claim older than this is a worker that died


def _intent_values(intent: FollowIntent | None) -> dict[str, str | None]:
    if intent is None:
        return {}
    return {"mode": intent.mode, "from_number": intent.from_number, "shelf_id": intent.shelf_id}


class ServiceLink(Protocol):
    """The connected service and the fetcher that carries its credential."""

    @property
    def service(self) -> Any: ...

    @property
    def fetcher(self) -> Any: ...


class ServiceNotConnectedError(Exception):
    """No connected service is configured: there is nothing to follow in."""


class FollowRefused(Exception):
    """The service is paused: nothing was sent and nothing was created."""

    code = "follow.service_paused"

    def __init__(self, waiting: str) -> None:
        super().__init__(f"the service is paused ({waiting})")
        self.waiting = waiting


class FollowConfirmationRequired(Exception):
    """Leaving the series out of the service's library asks for an explicit confirmation."""

    code = "follow.confirmation_required"


@dataclass(frozen=True)
class FollowOutcome:
    state: Literal["followed", "unfollowed", "in_progress"]
    sent: bool  # a call to the service was made by this one


class FollowRequests:
    def __init__(
        self,
        engine: sa.Engine,
        clock: Clock,
        open_link: Callable[[], ServiceLink | None],
        *,
        plugin_id: str = "suwayomi",
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._open = open_link
        self._plugin_id = plugin_id

    # -- follow -------------------------------------------------------------------------------

    async def follow(
        self,
        ref: SeriesRef,
        *,
        requester_user_id: str | None = None,
        visibility: str = "ops",
        intent: FollowIntent | None = None,
    ) -> FollowOutcome:
        """Approve the request for `ref` and make its entry, once. `intent`: what the add sheet
        chose (which Units, which shelf), kept with the request for the discovery to apply."""
        link = self._link()
        state, created = self._claim(ref, requester_user_id, visibility, intent)
        if state == "followed":
            return FollowOutcome("followed", sent=False)
        if state == "in_progress":
            return FollowOutcome("in_progress", sent=False)
        try:
            await self._probe(link)
            await from_service(link.service.set_follow(link.fetcher, ref, True))
        except BaseException:
            self._release(ref, forget=created)
            raise
        self._settle(ref, "followed")
        return FollowOutcome("followed", sent=True)

    # -- unfollow -----------------------------------------------------------------------------

    async def unfollow(
        self, ref: SeriesRef, *, remove_upstream: bool = False, confirmed: bool = False
    ) -> FollowOutcome:
        """Stop following. The series stays in the service unless `remove_upstream` is asked for,
        which needs `confirmed`."""
        if remove_upstream and not confirmed:
            raise FollowConfirmationRequired(
                "taking the series out of the service's library asks for a confirmation"
            )
        link = self._link()
        with self._engine.connect() as connection:
            self._refuse_if_paused(connection)
        await self._probe(link)
        await from_service(
            link.service.set_follow(link.fetcher, ref, False, remove_upstream=remove_upstream)
        )
        self._settle(ref, "unfollowed", only_if_known=True)
        return FollowOutcome("unfollowed", sent=True)

    # -- the record ---------------------------------------------------------------------------

    def _link(self) -> ServiceLink:
        link = self._open()
        if link is None:
            raise ServiceNotConnectedError("no connected service is configured")
        return link

    def _refuse_if_paused(self, connection: sa.Connection) -> None:
        pause = FailureLedger(self._plugin_id).paused(connection)
        if pause is not None:
            raise FollowRefused(pause.label)

    async def _probe(self, link: ServiceLink) -> None:
        """The fingerprint, as the cycle reads it before anything else: another installation is
        refused here, and the adapter only makes its category after a lately read fingerprint."""
        current = await from_service(link.service.instance_fingerprint(link.fetcher))
        with self._engine.begin() as connection:
            if check_instance(connection, self._plugin_id, current, self._clock.now()):
                raise ServiceInstanceChanged()

    def _claim(
        self,
        ref: SeriesRef,
        requester_user_id: str | None,
        visibility: str,
        intent: FollowIntent | None = None,
    ) -> tuple[str, bool]:
        """`(state, created)`: `followed` or `in_progress` when there is nothing for this one to
        do, else `following`, the claim it now holds. The pause is read in the same transaction."""
        immediate = self._engine.execution_options(shelfhold_begin="BEGIN IMMEDIATE")
        now = self._clock.now()
        with immediate.begin() as connection:
            self._refuse_if_paused(connection)
            requester_user_id = still_a_user(connection, requester_user_id)  # gone since: nobody's
            inserted = connection.execute(
                sqlite_insert(_REQUEST)
                .values(
                    id=new_ulid(),
                    source_key=ref.source_key,
                    remote_id=ref.remote_id,
                    state="approved",
                    visibility=visibility,
                    requester_user_id=requester_user_id,
                    **_intent_values(intent),
                )
                .on_conflict_do_nothing(index_elements=["source_key", "remote_id"])
            )
            row = connection.execute(self._row(ref)).one()
            if row.state == "followed":
                return "followed", False
            if intent is not None and inserted.rowcount == 0:  # the choice of this click is the one
                connection.execute(
                    sa.update(_REQUEST).where(_REQUEST.c.id == row.id).values(_intent_values(intent))
                )
            if row.state == "following" and row.claimed_at and now - row.claimed_at < CLAIM_TTL:
                return "in_progress", False
            connection.execute(
                sa.update(_REQUEST)
                .where(_REQUEST.c.id == row.id)
                .values(state="following", claimed_at=now, updated_at=now)
            )
            return "following", inserted.rowcount == 1

    def _release(self, ref: SeriesRef, *, forget: bool) -> None:
        """The call did not go through. A request this one made goes with it (nothing stale is
        kept for a series that does not exist or a service that refused); an older one stays
        approved, to be asked again."""
        with self._engine.begin() as connection:
            if forget:
                connection.execute(
                    sa.delete(_REQUEST).where(
                        _REQUEST.c.source_key == ref.source_key,
                        _REQUEST.c.remote_id == ref.remote_id,
                        _REQUEST.c.state == "following",
                    )
                )
                return
            connection.execute(
                sa.update(_REQUEST)
                .where(
                    _REQUEST.c.source_key == ref.source_key,
                    _REQUEST.c.remote_id == ref.remote_id,
                    _REQUEST.c.state == "following",
                )
                .values(state="approved", claimed_at=None, updated_at=self._clock.now())
            )

    def _settle(self, ref: SeriesRef, state: str, *, only_if_known: bool = False) -> None:
        now = self._clock.now()
        with self._engine.begin() as connection:
            result = connection.execute(
                sa.update(_REQUEST)
                .where(
                    _REQUEST.c.source_key == ref.source_key, _REQUEST.c.remote_id == ref.remote_id
                )
                .values(state=state, claimed_at=None, updated_at=now)
            )
            if result.rowcount == 0 and not only_if_known:  # pragma: no cover (claimed above)
                raise LookupError("the request vanished")

    @staticmethod
    def _row(ref: SeriesRef) -> sa.Select[Any]:
        return sa.select(_REQUEST).where(
            _REQUEST.c.source_key == ref.source_key, _REQUEST.c.remote_id == ref.remote_id
        )
