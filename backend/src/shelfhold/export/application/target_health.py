"""Health of a destination, as the core keeps it between two calls (`export_target.health`).

Plain data with a JSON form: the caller stores it, the client mutates it. Nothing secret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from shelfhold.events.waiting_state import WaitingState
from shelfhold.export.domain.kavita_compat import TargetMode
from shelfhold.export.domain.kavita_key_expiry import KeyNoticeState


@dataclass
class TargetHealth:
    version: str | None = None
    version_event_for: str | None = None  # version already announced as unsupported
    version_mode: TargetMode = TargetMode.FULL  # what the version alone says
    missing_routes: tuple[str, ...] = ()  # "METHOD /path", 404 seen
    paused: bool = False
    paused_route: str | None = None
    waiting_state: WaitingState | None = None
    key_expires_at: datetime | None = None
    key_notices: KeyNoticeState = field(default_factory=KeyNoticeState)
    warnings: tuple[str, ...] = ()  # known_bad_versions advisories (warning, never a block)
    install_id: str | None = None  # `installId` of the server, part of the instance fingerprint
    instance_changed: bool = False  # Kavita was reinstalled: exports wait for `target reconnect`
    not_indexed: bool = False  # a written file was never indexed (EF-EXP-034)
    child_exposure: bool = False  # a shelf of this target is paused by the access audit

    @property
    def mode(self) -> TargetMode:
        if self.version_mode is TargetMode.DEGRADED_READONLY:
            return TargetMode.DEGRADED_READONLY
        return TargetMode.DEGRADED_FEATURE if self.missing_routes else TargetMode.FULL

    def refresh_waiting(self) -> None:
        """The state a human must leave, by priority: forbidden, a changed instance, child
        exposure, route missing, not indexed, key expiring."""
        if self.paused:
            self.waiting_state = WaitingState.TARGET_FORBIDDEN
        elif self.instance_changed:
            self.waiting_state = WaitingState.INSTANCE_CHANGED
        elif self.child_exposure:
            self.waiting_state = WaitingState.CHILD_EXPOSURE
        elif self.missing_routes:
            self.waiting_state = WaitingState.ROUTE_MISSING
        elif self.not_indexed:
            self.waiting_state = WaitingState.NOT_INDEXED
        elif self.key_notices.sent:
            self.waiting_state = WaitingState.KEY_EXPIRING
        else:
            self.waiting_state = None

    def to_json(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "version_event_for": self.version_event_for,
            "version_mode": self.version_mode.value,
            "missing_routes": list(self.missing_routes),
            "paused": self.paused,
            "paused_route": self.paused_route,
            "waiting_state": self.waiting_state.value if self.waiting_state else None,
            "key_expires_at": self.key_expires_at.isoformat() if self.key_expires_at else None,
            "key_notices": {
                "expires_at": (
                    self.key_notices.expires_at.isoformat() if self.key_notices.expires_at else None
                ),
                "sent": sorted(self.key_notices.sent),
            },
            "warnings": list(self.warnings),
            "install_id": self.install_id,
            "instance_changed": self.instance_changed,
            "not_indexed": self.not_indexed,
            "child_exposure": self.child_exposure,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> TargetHealth:
        def moment(raw: str | None) -> datetime | None:
            return datetime.fromisoformat(raw) if raw else None

        notices = data.get("key_notices") or {}
        state = data.get("waiting_state")
        return cls(
            version=data.get("version"),
            version_event_for=data.get("version_event_for"),
            version_mode=TargetMode(data.get("version_mode", TargetMode.FULL.value)),
            missing_routes=tuple(data.get("missing_routes", ())),
            paused=bool(data.get("paused", False)),
            paused_route=data.get("paused_route"),
            waiting_state=WaitingState(state) if state else None,
            key_expires_at=moment(data.get("key_expires_at")),
            key_notices=KeyNoticeState(
                moment(notices.get("expires_at")), frozenset(notices.get("sent", ()))
            ),
            warnings=tuple(data.get("warnings", ())),
            install_id=data.get("install_id"),
            instance_changed=bool(data.get("instance_changed", False)),
            not_indexed=bool(data.get("not_indexed", False)),
            child_exposure=bool(data.get("child_exposure", False)),
        )
