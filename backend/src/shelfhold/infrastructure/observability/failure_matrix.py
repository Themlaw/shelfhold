"""The failure matrix of the dependencies (spec 7 bis, ENF-OPS-011) as data.

Every row names the event (EF-INT-006), the error code of the same identifier (EF-UX-003) and, when
a human has to act, the command that leaves the state (EF-INT-023). A test checks the rows against
the catalogs, and the simulator in `tests/faults` replays them. General rule: an infrastructure
failure pauses the queue concerned, counts no content error, and ends by itself when it can.
"""

from dataclasses import dataclass

# ruff: noqa: E501  (one row per line keeps the table reviewable against the spec)

M1A_BEHAVIOURS = frozenset(
    {
        "pause_exports",  # files already written stay, scans wait
        "pause_target",  # the destination is disabled until reconnected
        "pause_service",  # follow and fetch of one connected service
        "pause_shelf",  # one shelf or series
        "pause_queue",  # every write queue (disk)
        "block_writes",  # writes, deletions, scans and exports towards a root or the instance
        "degrade",  # degraded read-only or degraded feature
        "refuse_start",
        "notify_only",
    }
)


@dataclass(frozen=True)
class FailureRow:
    key: str
    dependency: str
    event: str
    behaviour: str
    needs_human: bool
    exit_command: str | None
    milestone: str  # first milestone whose emitter ships


def _row(
    key: str, dependency: str, event: str, behaviour: str, exit_command: str | None = None,
    milestone: str = "M1a", *, human: bool | None = None,
) -> FailureRow:  # fmt: skip
    needs = exit_command is not None if human is None else human
    return FailureRow(key, dependency, event, behaviour, needs, exit_command, milestone)


FAILURE_MATRIX: tuple[FailureRow, ...] = (
    _row("kavita_unreachable", "kavita", "target.unreachable", "pause_exports"),
    _row("kavita_key_revoked", "kavita", "target.unreachable", "pause_target", "target reconnect"),
    _row("kavita_version_unsupported", "kavita", "target.version_unsupported", "degrade"),
    _row("kavita_route_missing", "kavita", "target.route_missing", "degrade", "target recheck"),
    _row("kavita_reinstalled", "kavita", "target.instance_changed", "pause_target", "target reconnect"),
    _row("kavita_demoted", "kavita", "target.forbidden", "pause_target", "target reconnect"),
    _row("kavita_not_indexed", "kavita", "target.not_indexed", "notify_only", "jobs retry"),
    _row("kavita_key_expiring", "kavita", "target.key_expiring", "notify_only", "target rotate-key"),
    _row("kavita_updated", "kavita", "reconcile.drift", "notify_only", None, "M1b"),
    _row("kavita_child_exposure", "kavita", "target.child_exposure", "pause_shelf", "target recheck"),
    _row("series_dir_not_writable", "library", "target.permission_denied", "pause_shelf", "target recheck"),
    _row("suwayomi_unreachable", "suwayomi", "source.broken", "pause_service"),
    _row("suwayomi_auth_refused", "suwayomi", "source.auth_expired", "pause_service", "source set-secret"),
    _row("suwayomi_schema_changed", "suwayomi", "source.broken", "pause_service"),
    _row("suwayomi_version_unsupported", "suwayomi", "source.version_unsupported", "pause_service"),
    _row("suwayomi_multi_account", "suwayomi", "source.auth_mode_unsupported", "pause_service", "source recheck"),
    _row("suwayomi_mute", "suwayomi", "source.not_loaded", "degrade", None, "M1b"),
    _row("suwayomi_downloads_dropped", "suwayomi", "source.downloads_dropped", "degrade", None, "M1b"),
    _row("suwayomi_stale_series", "suwayomi", "series.stale", "notify_only", None, "M1b"),
    _row("suwayomi_reinstalled", "suwayomi", "source.instance_changed", "pause_service", "source reconnect", "M1b"),
    _row("sablier_slow", "sablier", "wake.deferred", "notify_only", None, "M1b"),
    _row("notifier_failing", "notifications", "notify.failed", "notify_only"),
    _row("metadata_api_down", "metadata", "metadata.provider_down", "notify_only", None, "M1b"),
    _row("disk_low", "disk", "disk.low", "pause_queue"),
    _row("safeguard_backlog", "store", "unit.safeguard_backlog", "notify_only", None, "M1b"),
    _row("mount_missing", "mount", "mount.sentinel_missing", "block_writes"),
    _row("root_missing", "mount", "mount.sentinel_missing", "block_writes"),
    _row("master_key_unreadable", "secrets", "secret.undecryptable", "pause_service"),
    _row("second_instance", "instance", "instance.lease_conflict", "refuse_start"),
    _row("restored_behind", "instance", "instance.restored_behind", "block_writes", "reconcile --adopt-after-restore"),
    _row("foreign_writer", "library", "target.foreign_writer_detected", "pause_shelf", "series resume"),
    _row("job_dead_lettered", "jobs", "job.dead_lettered", "notify_only", "jobs retry"),
    _row("tombstone_key_unknown", "secrets", "tombstone.key_unknown", "block_writes", "secrets import"),
)  # fmt: skip


def rows_for(milestone: str) -> tuple[FailureRow, ...]:
    return tuple(row for row in FAILURE_MATRIX if row.milestone == milestone)
