# SPDX-License-Identifier: MIT
"""The Tracker contract (M6, EF-INT-010): one remote reading account per profile.

A tracker plugin speaks for one reading service a profile has an account on. The host calls it
with a Fetcher that already carries the account's credential (EF-SRC-023): the plugin never sees
the token, only the requests it makes with it. `link` turns the account into a `TrackerRef` the
host keeps; `push_progress` sends where the profile is; `pull` brings back a `TrackerHint` —
where the profile is on the remote service, nothing more. A hint is never a read mark: the host
shows it and writes nothing (02 section 4.4).

Nothing in this module imports the AGPL core (ENF-LEG-010).
"""

from datetime import datetime
from typing import Any, Protocol

from pydantic import Field

from shelfhold_sdk.fetcher import Fetcher
from shelfhold_sdk.models import AuthFlow, PluginManifest, SdkModel, SessionState

__all__ = [
    "TrackProgress",
    "Tracker",
    "TrackerHint",
    "TrackerManifest",
    "TrackerRef",
]


class TrackerManifest(PluginManifest):
    """The manifest of a tracker plugin. `auth` says how the host injects the account's token
    (EF-SRC-023): the flow the Fetcher speaks, never the value. The `api` domain of the
    permissions is the origin the credential is sent to."""

    auth: AuthFlow | None = None


class TrackerRef(SdkModel):
    """What `link` returned: the account on the remote service, as opaque as the plugin wants.
    The host stores it as JSON and hands it back as it was."""

    remote_id: str
    opaque: dict[str, Any] = Field(default_factory=dict)


class TrackProgress(SdkModel):
    """Where the profile is: a fraction of the carrier, the time of the observation, and whatever
    the plugin needs to place it (a chapter, a page)."""

    fraction: float = Field(ge=0.0, le=1.0)
    at: datetime | None = None
    opaque: dict[str, Any] = Field(default_factory=dict)


class TrackerHint(SdkModel):
    """What `pull` brought back: where the profile is on the remote service. A hint for the
    reader, never a read mark: the host writes none of it (EF-INT-010)."""

    fraction: float | None = Field(default=None, ge=0.0, le=1.0)
    at: datetime | None = None
    opaque: dict[str, Any] = Field(default_factory=dict)


class Tracker(Protocol):
    """The contract of a tracker plugin (02 section 4.4). Per user, refused to a child profile
    by default (EF-INT-010)."""

    manifest: TrackerManifest

    async def link(self, f: Fetcher, account: SessionState, ids: dict[str, Any]) -> TrackerRef:
        """Turn the account into a reference the host keeps. `account` carries no secret: the
        token travels in `f`, which the host wrapped for this profile."""
        ...

    async def push_progress(self, f: Fetcher, ref: TrackerRef, p: TrackProgress) -> None:
        """Send where the profile is. Called only once the profile said yes to the push."""
        ...

    async def pull(self, f: Fetcher, ref: TrackerRef) -> TrackerHint | None:
        """Bring back where the profile is on the remote service, or `None` when the service
        knows nothing. A hint, never a read mark."""
        ...
