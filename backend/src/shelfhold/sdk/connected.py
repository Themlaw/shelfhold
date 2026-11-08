# SPDX-License-Identifier: MIT
"""The ConnectedService port above Source (EF-SRC-033, EF-SRC-034, ADR-0041).

A connected service drives a stateful third-party tool. What is specific to it goes through
optional, declared capabilities; the core programs against them and never names a tool.
"""

from enum import Flag, auto
from typing import Annotated, Literal, Protocol

from pydantic import PlainSerializer, PlainValidator, WithJsonSchema

from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.models import SdkModel, flag_names, flag_parser
from shelfhold.sdk.refs import RangedPayload, RangeRef, SeriesRef, UnitPayload, UnitRef
from shelfhold.sdk.source import Source


class ConnectedCapability(Flag):
    CATEGORIES = auto()  # follow by category, exclusion from automatic download
    SERVER_QUEUE = auto()  # two-step acquisition: request -> ticket, poll, fetch, cancel
    RELEASE_CLAIM = auto()  # purge or drop of the claim (EF-BIB-038, EF-SRC-031)
    UPSTREAM_RESTORE = auto()  # snapshot and restore of upstream settings (EF-EXP-056)
    HEALTH_PROBE = auto()  # functional probe (EF-SRC-026)
    AUTH_MODE = auto()  # authentication mode read at add and at each probe (EF-SRC-031)
    WAKE = auto()  # delegated to the host WakeHook (EF-INT-022)
    INSTANCE_FINGERPRINT = auto()  # M1b (EF-SRC-036)


ConnectedCapabilitySet = Annotated[
    ConnectedCapability,
    PlainValidator(flag_parser(ConnectedCapability)),
    PlainSerializer(flag_names, return_type=list[str]),
    WithJsonSchema({"type": "array", "items": {"type": "string"}}),
]

TicketState = Literal["queued", "downloading", "ready", "failed", "gone"]


class Ticket(SdkModel):
    """Opaque handle of a request made to the server queue; one ticket per target."""

    token: str
    target_kind: Literal["unit", "range"]


class ServiceHealth(SdkModel):
    ok: bool
    version: str | None = None
    detail: str | None = None


class ReleaseResult(SdkModel):
    released: bool
    # Measured after the release, never assumed (EF-SRC-031): the bytes the service really freed
    # and whether the file is still there (another account may keep it). None: not measured.
    freed_bytes: int | None = None
    still_present: bool | None = None


class ConnectedService(Source, Protocol):
    """Methods exist only when the matching capability is declared in `connected`."""

    connected: ConnectedCapability
    wake_target: str | None

    # CATEGORIES
    async def followed_series(self, f: Fetcher) -> list[SeriesRef]: ...

    async def set_follow(
        self, f: Fetcher, ref: SeriesRef, on: bool, *, remove_upstream: bool = False
    ) -> None:
        """Idempotent. `on=False` stops the hold of Shelfhold and keeps the series in the tool,
        unless `remove_upstream` asks for it explicitly (EF-SRC-029)."""
        ...

    # SERVER_QUEUE: the target is polymorphic, a Unit or a range (EF-SRC-034)
    async def request(self, f: Fetcher, target: UnitRef | RangeRef) -> Ticket: ...

    async def poll(self, f: Fetcher, t: Ticket) -> TicketState: ...

    async def fetch(self, f: Fetcher, t: Ticket) -> UnitPayload | RangedPayload: ...

    async def cancel(self, f: Fetcher, t: Ticket) -> None: ...

    # RELEASE_CLAIM
    async def release(self, f: Fetcher, t: Ticket) -> ReleaseResult: ...

    # UPSTREAM_RESTORE
    async def snapshot_upstream(self, f: Fetcher, ref: SeriesRef) -> dict[str, object]: ...

    async def restore_upstream(
        self, f: Fetcher, ref: SeriesRef, snap: dict[str, object]
    ) -> None: ...

    # HEALTH_PROBE
    async def semantic_health(self, f: Fetcher) -> ServiceHealth: ...

    async def request_refresh(self, f: Fetcher, refs: list[SeriesRef]) -> None: ...

    # AUTH_MODE
    async def auth_mode(self, f: Fetcher) -> str: ...

    # INSTANCE_FINGERPRINT (M1b)
    async def instance_fingerprint(self, f: Fetcher) -> str:
        """What tells this installation of the tool from another; `""` when it cannot say yet."""
        ...
