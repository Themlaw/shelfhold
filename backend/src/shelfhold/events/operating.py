"""Operating events notifiable by default (EF-INT-006): the single list the error catalog follows.

Each event has an error code of the same identifier (EF-UX-003, ENF-OPS-011), and conversely each
failure-matrix code is one of these events. Source: 01 EF-INT-006 (passes 3 to 13 and the Q3e
follow events). `M1B` marks events whose emitter ships in M1b; their codes are stable from M1a.
"""

from shelfhold.events.waiting_state import WaitingState

M1B: frozenset[str] = frozenset(
    {
        "series.stale",
        "source.downloads_dropped",
        "source.instance_changed",
        "source.not_loaded",
        "tombstone.journal_truncated",
        "unit.safeguard_backlog",
        "unit.safeguard_failed",
    }
)

OPERATING_EVENTS: frozenset[str] = frozenset(
    {
        "backup.failed",
        "backup.stale",
        "disk.low",
        "instance.lease_conflict",
        "instance.restored_behind",
        "job.dead_lettered",
        "metadata.provider_down",
        "mount.sentinel_missing",
        "notify.failed",
        "reconcile.drift",
        "secret.undecryptable",
        "series.gap",
        "series.stale",
        "series.removed_upstream",
        "series.tombstoned_upstream",
        "source.auth_expired",
        "source.auth_mode_unsupported",
        "source.broken",
        "source.dns_blocked",
        "source.downloads_dropped",
        "source.fallback",
        "source.instance_changed",
        "source.not_loaded",
        "source.version_unsupported",
        "target.child_exposure",
        "target.forbidden",
        "target.foreign_writer_detected",
        "target.instance_changed",
        "target.key_expiring",
        "target.not_indexed",
        "target.permission_denied",
        "target.route_missing",
        "target.unreachable",
        "target.version_unsupported",
        "tombstone.journal_truncated",
        "tombstone.key_unknown",
        "tombstone.migration_hit",
        "tombstone.root_mismatch",
        "tombstone.unverified",
        "unit.safeguard_backlog",
        "unit.safeguard_failed",
        "wake.deferred",
    }
)

# Waiting state (EF-INT-023) -> the event that announces it. A state absent here is in
# `WAITING_WITHOUT_EVENT`: a test checks that the two partition `WaitingState` exactly.
WAITING_STATE_EVENT: dict[WaitingState, str] = {
    WaitingState.PAUSED_FOREIGN_WRITER: "target.foreign_writer_detected",
    WaitingState.DEAD_LETTER: "job.dead_lettered",
    WaitingState.INSTANCE_CHANGED: "target.instance_changed",
    WaitingState.TARGET_FORBIDDEN: "target.forbidden",
    WaitingState.KEY_EXPIRING: "target.key_expiring",
    WaitingState.NOT_INDEXED: "target.not_indexed",
    WaitingState.AUTH_EXPIRED: "source.auth_expired",
    WaitingState.PERMISSION_DENIED: "target.permission_denied",
    WaitingState.CHILD_EXPOSURE: "target.child_exposure",
    WaitingState.AUTH_MODE_UNSUPPORTED: "source.auth_mode_unsupported",
    WaitingState.RESTORED_BEHIND: "instance.restored_behind",
    WaitingState.TOMBSTONE_KEY_UNKNOWN: "tombstone.key_unknown",
    WaitingState.ROUTE_MISSING: "target.route_missing",
    WaitingState.SAFEGUARD_BACKLOG: "unit.safeguard_backlog",
    WaitingState.SOURCE_INSTANCE_CHANGED: "source.instance_changed",
}
WAITING_WITHOUT_EVENT: frozenset[WaitingState] = frozenset(
    set(WaitingState) - set(WAITING_STATE_EVENT)
)

# `notify.failed` is the banner of a notification that could not be delivered (EF-INT-006): it is
# an event and an error code, but Apprise never carries it, or a broken channel would report itself
# through the same broken channel.
NOT_NOTIFIED: frozenset[str] = frozenset({"notify.failed"})

# Events that are aggregated into one message while several wait (EF-INT-006).
AGGREGATED: frozenset[str] = frozenset({"job.dead_lettered", "series.stale"})
