"""The request service of the application: its ports over the sources, the connected service and
the shelves (EF-USR-010, EF-SRC-029).

`build_ports` is the one place that knows how an approval reaches them; the tests replace it. A
series of the connected service (or of a source that hands over images) is followed, the next
follow cycle makes its Work; any other result is acquired at once. The estimate is what the source
lists: the sources do not tell a size in bytes, so only the number of Units can cross the
threshold of 5 GiB (the confirmation is by count there).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

import sqlalchemy as sa
import structlog

from shelfhold.acquisition.application.requests import (
    Estimate,
    Ports,
    PreparedAcquisition,
    RequestRefusedError,
    RequestService,
    SourceRef,
)
from shelfhold.catalog.public import RatingRefusedError, RatingService
from shelfhold.events.bus import PublishedEvent
from shelfhold.export.application.shelves import ShelfService
from shelfhold.identity.public import Principal
from shelfhold.ops.acquire import AcquireOps
from shelfhold.ops.errors import CodedRefusal
from shelfhold.ops.follows import FollowOps
from shelfhold.sdk.age import AgeRating
from shelfhold.sdk.source import SourceManifest

if TYPE_CHECKING:
    from shelfhold.composition import Composition

__all__ = ["build_ports", "build_service", "resolve_request_ref", "subscribe_arrivals"]

CONNECTED = "suwayomi"  # the key of the connected service: its series are followed


@dataclass(frozen=True)
class _Approver:
    """The admin behind an approval, for the audit line of a shelf."""

    actor_id: str | None
    session_id: str | None = None
    auth_time: datetime | None = None


class _Ports:
    def __init__(self, comp: Composition) -> None:
        self._comp = comp
        self._shelves = ShelfService(comp.engine, clock=comp.clock)
        self._rating = RatingService(fresh_auth_s=comp.settings.fresh_auth_s)
        self._counted: dict[tuple[str, str], int] = {}  # what `estimate` just asked the source

    def kind_of(self, source_key: str) -> str:
        if source_key.partition(":")[0] == CONNECTED:
            return "manga"
        registry = self._comp.plugins()
        if registry is not None:
            for record in registry.active():
                manifest = record.manifest
                if record.key == source_key and isinstance(manifest, SourceManifest):
                    return "manga" if set(manifest.content_kinds) == {"images"} else "novel"
        return "novel"

    def estimate(self, source_key: str, remote_id: str) -> Estimate:
        units = AcquireOps(self._comp).unit_count(source_key, remote_id)
        self._counted[(source_key, remote_id)] = units  # the space gate of the approval reuses it
        return Estimate(units, None)

    def follow(
        self, source_key: str, remote_id: str, *, requester_user_id: str | None, visibility: str
    ) -> None:
        FollowOps(self._comp).follow(
            source_key,
            remote_id,
            requester_user_id=requester_user_id,
            visibility=visibility,
            units=self._counted.get((source_key, remote_id)),
        )

    def prepare(
        self, source_key: str, remote_id: str, *, requester_user_id: str | None, visibility: str
    ) -> PreparedAcquisition:
        prepared = AcquireOps(self._comp).prepare(
            url=None, source_key=source_key, remote_id=remote_id, unit=None,
            requester_user_id=requester_user_id, visibility=visibility,
            units=self._counted.get((source_key, remote_id)),
        )  # fmt: skip
        return PreparedAcquisition(prepared.work_id, prepared.edition_id, prepared.request_id)

    def start(self, handle: str | None) -> None:
        AcquireOps(self._comp).enqueue(handle)

    def proposed_shelf(self, edition_id: str) -> str | None:
        return self._shelves.proposed_shelf(edition_id)

    def settle(
        self, work_id: str, edition_id: str | None, rating: AgeRating | None,
        shelf_id: str | None, *, principal: Principal,
    ) -> str | None:  # fmt: skip
        """Rate then file, in one transaction: a family shelf only admits an Edition that is
        rated, and a refused shelf must leave no rating behind."""
        now = self._comp.clock.now()
        try:
            with self._shelves.transaction() as connection:
                if rating is not None:
                    self._rating.rate(connection, work_id, rating, principal=principal, now=now)
                if edition_id is None:
                    return None
                result, used = self._shelves.file_in(
                    connection, edition_id, shelf_id, actor=_Approver(principal.actor_id)
                )
                if not result.filed and shelf_id is not None:
                    raise RequestRefusedError("shelf_refused")
                return used if result.filed else None
        except RatingRefusedError:
            raise RequestRefusedError("not_allowed") from None
        except (LookupError, sa.exc.NoResultFound):  # no such Edition or shelf
            raise RequestRefusedError("shelf_refused") from None


def build_ports(comp: Composition) -> Ports:
    return _Ports(comp)


def build_service(comp: Composition) -> RequestService:
    return RequestService(
        comp.engine, comp.clock, build_ports(comp), bus=comp.bus,
        fresh_auth_s=comp.settings.fresh_auth_s,
    )  # fmt: skip


def resolve_request_ref(acquire: AcquireOps, token: str, owner: str) -> SourceRef | None:
    """The result a child's search showed her, if the door is still open for her: her own
    reference (not a sibling's, not a forged one, not an expired one), of a source that is still
    family and that her policy still allows. None for all of them alike."""
    try:
        ref = acquire.resolve_family_ref(token, owner=owner)
    except CodedRefusal:
        return None
    if not acquire.family_door_open(owner, ref.source_key):
        return None
    return SourceRef(ref.source_key, ref.remote_id, ref.title or "-", dict(ref.memo))


def subscribe_arrivals(comp: Composition, service: RequestService) -> None:
    """A Unit was filed: the approved requests for its Work have arrived. The handler runs after
    the filing committed and never raises into it."""
    log = structlog.get_logger()

    def on_stored(event: PublishedEvent) -> None:
        work_id = event.payload.get("work_id")
        if not isinstance(work_id, str):
            return
        try:
            service.arrived(work_id)
        except Exception:  # a request that cannot be marked must not undo a filing
            log.warning("request.arrival_failed")

    comp.bus.subscribe(on_stored, event_type="unit.stored")
