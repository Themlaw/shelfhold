"""The typed event bus (EF-INT-006, ENF-OBS-005, ENF-SEC-015).

An event is a row of `event` (append-only, the SSE sequence). `publish` takes the caller's open
connection: the event row and, for an operating event, one `notification_pending` row per
configured channel are written in the same transaction, so a crash between the event and the
send loses neither (the sender is a separate idempotent job, `integrations.apprise`). The bus
imports no integration: the channels come from a `ChannelSource`, so `events` stays below
`integrations`.

An event carries identifiers and codes only: a ULID under a key that ends with `_id`, a value of
the enumeration of the event type under `reason`, `kind`, `state`, `code`, a code under `job_kind`,
a number or a boolean. Never a title or free text, which the atelier resolves at display time from
the ULID (PLAN-M1a L13, 02 section 11).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Protocol

import sqlalchemy as sa
import structlog
from sqlalchemy import Connection

from shelfhold.events.operating import NOT_NOTIFIED, OPERATING_EVENTS
from shelfhold.events.waiting_state import WaitingState
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform

log = structlog.get_logger(__name__)

SCHEMA_VERSION: Final = 1
VISIBILITIES: Final = ("private", "household", "ops")
_ULID: Final = re.compile(r"[0-9A-HJKMNP-TV-Z]{26}\Z")
_CODE: Final = re.compile(r"[a-z][a-z0-9_]*(?:[.:-][a-z0-9_]+)*\Z")
_ENUM_KEYS: Final = ("reason", "kind", "state", "code", "error_code", "scope", "volume")
_FREE_CODE_KEYS: Final = frozenset({"job_kind"})  # a job kind is named in code, never typed in
GENERIC_REASONS: Final = frozenset(
    {
        "unreachable", "timeout", "unauthorized", "forbidden", "not_found", "http_503",
        "version_unsupported", "schema_mismatch", "retries_exhausted", "handler_error", "unknown",
    }
)  # fmt: skip
VOLUME_VALUES: Final = frozenset(
    {"data", "store", "library", *(f"library_{n}" for n in range(2, 17))}
)
# Per event type, the values each enumerated key may take. A type or a key that is not listed
# takes the default of that key; a value outside the enumeration is refused, so no title can be
# smuggled in as a "code" (ENF-SEC-015). `reason` defaults to GENERIC_REASONS; the others to none.
PAYLOAD_ENUMS: Final[dict[str, dict[str, frozenset[str]]]] = {
    "notify.failed": {"reason": frozenset({"retries_exhausted"})},
    "job.dead_lettered": {"reason": frozenset({"retries_exhausted", "handler_error"})},
    "ops.window_missed": {"reason": frozenset({"window_over"})},
    # EF-SRC-024: an Edition that may still be protected where the instance is; never blocking.
    "ops.rights_warning": {
        "reason": frozenset({"may_be_protected", "translation_may_be_protected"})
    },
    # EF-BIB-053 (3): why a tombstone line is inert; never a value of the line.
    "tombstone.unverified": {
        "reason": frozenset(
            {"authentication_failed", "kid_unregistered", "lost_keyring", "malformed"}
        )
    },
    "mount.sentinel_missing": {"reason": frozenset({"sentinel", "missing_ratio"})},
    "reconcile.drift": {
        "kind": frozenset(
            {"unknown_file", "adopted_missing", "unsettled_intent", "store_missing", "store_lost"}
        )
    },
    # EF-INT-015: why a backup failed (a closed list, never a path or a message)
    "backup.failed": {
        "reason": frozenset(
            {
                "passphrase_missing",
                "passphrase_invalid",
                "passphrase_colocated",
                "destination_missing",
                "destination_unavailable",
                "destination_not_distinct",
                "no_room",
                "too_large",
                "busy",
                "keyring_unavailable",
                "database_error",
                "write_failed",
                "verify_failed",
                "interrupted",
                "locked",
            }
        )
    },
    # a key that does not open: the keyring file itself, never a key or a path
    "secret.undecryptable": {
        "reason": frozenset(
            {"keyring_unreadable", "keyring_corrupt", "keyring_missing", "keyring_permissions"}
        )
    },
    "target.permission_denied": {"reason": GENERIC_REASONS},
    # EF-BIB-051 (6): why an archive was refused, never the name of an entry.
    "content.untrusted_entry": {
        "reason": frozenset(
            {
                "duplicate_comicinfo",
                "magic_mismatch",
                "no_pages",
                "not_allowlisted",
                "unsafe_archive",
                "unsafe_xml",
            }
        )
    },
    # EF-EXP-016: why a served file was replaced at its path (`export_upgrade.reason`).
    "unit.upgraded": {
        "reason": frozenset({"better_version", "preferred_team", "integrity_reacquire", "restore"})
    },
    # ENF-OPS-006: the volume under its free-space mark.
    "disk.low": {"volume": VOLUME_VALUES},
    "ops.disk_recovered": {"volume": VOLUME_VALUES},
}
CODE_VALUES: Final = frozenset(
    OPERATING_EVENTS | {"job.failed", "job.unknown_kind", "window.preempted"}
)
KIND_VALUES: Final = frozenset({"manual", "scheduled", "cbz", "epub"})
STATE_VALUES: Final = frozenset(str(s.value) for s in WaitingState)
SCOPE_VALUES: Final = frozenset({"variant", "place", "series_binding", "work"})  # tombstones
_DEFAULT_ENUMS: Final[dict[str, frozenset[str]]] = {
    "reason": GENERIC_REASONS,
    "kind": KIND_VALUES,
    "state": STATE_VALUES,
    "code": CODE_VALUES,
    "error_code": CODE_VALUES,
    "scope": SCOPE_VALUES,
    "volume": frozenset(),
}
_EXTENSION_TYPE: Final = re.compile(r"plugin\.[a-z0-9_]+\.[a-z0-9_.]+\Z")
_OPS_TYPE: Final = re.compile(r"ops\.[a-z0-9_]+\Z")
_MAX_CODE: Final = 64


class UnknownEventType(ValueError):
    """The type is neither an operating event, an `ops.*` internal one nor a plugin namespace."""


class EventPayloadError(ValueError):
    """The payload carries something other than identifiers, codes and numbers."""


class ChannelSource(Protocol):
    def channel_names(self, connection: Connection) -> Sequence[str]:
        """The notification channels configured now, read inside the publisher's transaction."""
        ...


@dataclass(frozen=True)
class PublishedEvent:
    id: int
    type: str
    payload: Mapping[str, Any]
    visibility: str
    channels: tuple[str, ...]  # the channels a pending row was written for


Subscriber = Callable[[PublishedEvent], None]


def check_event_type(event_type: str) -> None:
    if (
        event_type in OPERATING_EVENTS
        or _OPS_TYPE.fullmatch(event_type)
        or _EXTENSION_TYPE.fullmatch(event_type)
    ):
        return
    raise UnknownEventType(event_type)


def check_payload(payload: Mapping[str, object], event_type: str | None = None) -> None:
    """Refuse anything that could be a title: only ULIDs, codes, numbers and booleans pass."""
    for key, value in payload.items():
        if not re.fullmatch(r"[a-z][a-z0-9_]*", key):
            raise EventPayloadError("payload keys are lowercase identifiers")
        if value is None or isinstance(value, bool | int | float):
            continue
        if not isinstance(value, str):
            raise EventPayloadError(f"{key}: only scalars are allowed in an event payload")
        if key.endswith("_id"):
            if not _ULID.fullmatch(value):
                raise EventPayloadError(f"{key} must be a ULID")
        elif key in _ENUM_KEYS:
            allowed = PAYLOAD_ENUMS.get(event_type or "", {}).get(key, _DEFAULT_ENUMS[key])
            if value not in allowed:
                raise EventPayloadError(f"{key} is not one of the values of this event type")
        elif key in _FREE_CODE_KEYS:
            if len(value) > _MAX_CODE or not _CODE.fullmatch(value):
                raise EventPayloadError(f"{key} must be a lowercase code")
        else:
            raise EventPayloadError(f"{key}: a text value is not allowed in an event payload")


class EventBus:
    def __init__(self, clock: Clock, *, channels: ChannelSource | None = None) -> None:
        self._clock = clock
        self._channels = channels
        self._subscribers: list[tuple[str | None, Subscriber]] = []

    def subscribe(self, handler: Subscriber, *, event_type: str | None = None) -> None:
        """`handler` runs in `deliver`, after the publisher's commit, never in its transaction."""
        if event_type is not None:
            check_event_type(event_type)
        self._subscribers.append((event_type, handler))

    def publish(
        self,
        connection: Connection,
        event_type: str,
        payload: Mapping[str, object] | None = None,
        *,
        actor_id: str | None = None,
        visibility: str = "ops",
    ) -> PublishedEvent:
        check_event_type(event_type)
        data = dict(payload or {})
        check_payload(data, event_type)
        if visibility not in VISIBILITIES:
            raise ValueError(f"unknown visibility {visibility!r}")
        now = self._clock.now()
        inserted = connection.execute(
            sa.insert(platform.event).values(
                type=event_type,
                schema_version=SCHEMA_VERSION,
                payload=data,
                occurred_at=now,
                actor_id=actor_id,
                visibility=visibility,
            )
        ).inserted_primary_key
        if inserted is None:
            raise RuntimeError("the event row has no key")
        event_id = int(inserted[0])
        channels: tuple[str, ...] = ()
        if self._notifiable(event_type, visibility) and self._channels is not None:
            channels = tuple(self._channels.channel_names(connection))
            for channel in channels:
                connection.execute(
                    sa.insert(platform.notification_pending).values(
                        id=new_ulid(), event_id=event_id, channel=channel, next_try_at=now
                    )
                )
        log.info("event.published", event_id=event_id, event_type=event_type)
        return PublishedEvent(event_id, event_type, data, visibility, channels)

    def deliver(self, events: Sequence[PublishedEvent]) -> None:
        """Hand committed events to in-process subscribers; one failing handler stops no other."""
        for published in events:
            for wanted, handler in self._subscribers:
                if wanted is None or wanted == published.type:
                    try:
                        handler(published)
                    except Exception:
                        log.warning(
                            "event.subscriber_failed",
                            event_id=published.id,
                            event_type=published.type,
                        )

    @staticmethod
    def _notifiable(event_type: str, visibility: str) -> bool:
        return (
            visibility == "ops"
            and event_type in OPERATING_EVENTS
            and event_type not in NOT_NOTIFIED
        )
