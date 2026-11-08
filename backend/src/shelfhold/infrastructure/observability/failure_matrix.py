"""The failure matrix of the dependencies (spec 7 bis, ENF-OPS-011) as data.

Every row names the event (EF-INT-006), the error code of the same identifier (EF-UX-003) and, when
a human has to act, the command that leaves the state (EF-INT-023). A test checks the rows against
the catalogs. The simulator in `tests/faults` replays the rows marked `simulated`; every other
row names the real test (or the lot) that proves it in `proof`. General rule: an infrastructure
failure pauses the queue concerned, counts no content error, and ends by itself when it can.
"""

from dataclasses import dataclass, replace

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
    simulated: bool = False  # replayed by `tests/faults/test_failure_matrix.py`
    proof: str = ""  # not simulated: the real test, or the lot that ships the emitter


def _row(
    key: str, dependency: str, event: str, behaviour: str, exit_command: str | None = None,
    milestone: str = "M1a", *, human: bool | None = None,
) -> FailureRow:  # fmt: skip
    needs = exit_command is not None if human is None else human
    return FailureRow(key, dependency, event, behaviour, needs, exit_command, milestone)


_ROWS: tuple[FailureRow, ...] = (
    _row("kavita_unreachable", "kavita", "target.unreachable", "pause_exports"),
    _row("kavita_key_revoked", "kavita", "target.unreachable", "pause_target", "target reconnect"),
    _row("kavita_version_unsupported", "kavita", "target.version_unsupported", "degrade"),
    _row("kavita_route_missing", "kavita", "target.route_missing", "degrade", "target recheck"),
    _row("kavita_reinstalled", "kavita", "target.instance_changed", "pause_target", "target reconnect"),
    _row("kavita_demoted", "kavita", "target.forbidden", "pause_target", "target reconnect"),
    _row("kavita_not_indexed", "kavita", "target.not_indexed", "notify_only", "jobs retry"),
    _row("kavita_key_expiring", "kavita", "target.key_expiring", "notify_only", "target rotate-key"),
    _row("kavita_updated", "kavita", "reconcile.drift", "notify_only"),
    _row("kavita_child_exposure", "kavita", "target.child_exposure", "pause_shelf", "target recheck"),
    _row("series_dir_not_writable", "library", "target.permission_denied", "pause_shelf", "target recheck"),
    _row("suwayomi_unreachable", "suwayomi", "source.broken", "pause_service"),
    _row("suwayomi_auth_refused", "suwayomi", "source.auth_expired", "pause_service", "source set-secret"),
    _row("suwayomi_schema_changed", "suwayomi", "source.version_unsupported", "pause_service"),
    _row("suwayomi_version_unsupported", "suwayomi", "source.version_unsupported", "pause_service"),
    _row("suwayomi_multi_account", "suwayomi", "source.auth_mode_unsupported", "pause_service", "source recheck"),
    _row("suwayomi_mute", "suwayomi", "source.not_loaded", "degrade", None, "M1b"),
    _row("suwayomi_downloads_dropped", "suwayomi", "source.downloads_dropped", "degrade", None, "M1b"),
    _row("suwayomi_stale_series", "suwayomi", "series.stale", "notify_only", None, "M1b"),
    _row("suwayomi_reinstalled", "suwayomi", "source.instance_changed", "pause_service", "source reconnect", "M1b"),  # closed: see _CLOSED
    _row("sablier_slow", "sablier", "wake.deferred", "notify_only", None, "M1b"),
    _row("notifier_failing", "notifications", "notify.failed", "notify_only"),
    _row("metadata_api_down", "metadata", "metadata.provider_down", "notify_only", None, "M1b"),
    _row("disk_low", "disk", "disk.low", "pause_queue"),
    _row("safeguard_backlog", "store", "unit.safeguard_backlog", "notify_only", None, "M1b"),
    _row("mount_missing", "mount", "mount.sentinel_missing", "block_writes"),
    _row("root_missing", "mount", "mount.sentinel_missing", "block_writes"),
    _row("master_key_unreadable", "secrets", "secret.undecryptable", "pause_service"),
    _row("lncrawl_unreachable", "lncrawl", "source.broken", "pause_service", None, "M1b"),
    _row("store_missing_after_restore", "store", "reconcile.drift", "notify_only", None, "M1b"),
    _row("kavita_asleep_sablier", "sablier", "wake.deferred", "notify_only", None, "M1b"),
    _row("second_instance", "instance", "instance.lease_conflict", "refuse_start"),
    _row("restored_behind", "instance", "instance.restored_behind", "block_writes", "reconcile --adopt-after-restore"),
    _row("foreign_writer", "library", "target.foreign_writer_detected", "pause_shelf", "series resume"),
    _row("job_dead_lettered", "jobs", "job.dead_lettered", "notify_only", "jobs retry"),
    _row("tombstone_key_unknown", "secrets", "tombstone.key_unknown", "block_writes", "secrets import"),
    _row("tombstone_journal_truncated", "store", "tombstone.journal_truncated", "block_writes", None, "M1b"),
    _row("tombstone_root_pending", "mount", "tombstone.root_mismatch", "block_writes"),
    _row("tombstone_sentinel_forged", "store", "tombstone.unverified", "notify_only"),
    _row("dns_blocked", "network", "source.dns_blocked", "pause_service", "source recheck"),
    _row("abrupt_stop", "instance", "reconcile.drift", "notify_only"),
)  # fmt: skip


SIMULATED = frozenset(
    {"kavita_unreachable", "suwayomi_unreachable", "disk_low", "second_instance",
     "series_dir_not_writable", "mount_missing"}
)  # fmt: skip
K = "tests/unit/test_kavita_client.py"
AUDIT = "tests/integration/test_kavita_reinstall_audit.py"
FOREIGN = "tests/integration/test_foreign_writer_reconcile.py"
SUWAYOMI = "tests/unit/test_suwayomi_adapter.py"
FOLLOW = "tests/integration/test_follow_cycle.py"
OPS_EVENTS = "tests/integration/test_l52_ops_events.py"
KEYRING = "tests/property/test_tombstone_keyring.py"
# The M1a rows that `tests/faults` does not replay: the pytest node id of the test that does,
# which names the event of the row (a test checks that the node exists and names it).
_REAL_TESTS = {
    "kavita_key_revoked": f"{K}::test_kavita_401_revoked_key_pauses_destination_with_target_unreachable",
    "kavita_version_unsupported": f"{K}::test_unsupported_kavita_version_degrades_to_plan_only",
    "kavita_route_missing": f"{K}::test_kavita_route_404_sets_degraded_mode_never_plans_without_collision_check",
    "kavita_reinstalled": f"{AUDIT}::test_kavita_reinstall_emits_instance_changed_and_pauses",
    "kavita_demoted": f"{K}::test_kavita_403_pauses_destination_target_forbidden",
    "kavita_not_indexed": f"{AUDIT}::test_written_but_not_indexed_emits_target_not_indexed",
    "kavita_key_expiring": f"{K}::test_key_expiring_event_j14_j1",
    "kavita_updated": "tests/integration/test_kavita_age_push.py::test_modified_field_in_kavita_emits_drift_and_is_pushed_again",
    "kavita_child_exposure": f"{AUDIT}::test_daily_audit_pauses_shelf_on_child_exposure",
    "suwayomi_auth_refused": f"{FOLLOW}::test_suwayomi_401_pauses_service_auth_expired_no_failure_counted",
    "suwayomi_schema_changed": f"{FOLLOW}::test_suwayomi_schema_mismatch_pauses_without_counting",
    "suwayomi_version_unsupported": f"{SUWAYOMI}::test_suwayomi_version_out_of_range_reported",
    "suwayomi_multi_account": f"{SUWAYOMI}::test_unsupported_auth_mode_pauses_with_auth_mode_unsupported",
    "notifier_failing": "tests/integration/test_notifications_outbox.py::test_failed_send_is_retried_then_reported_as_notify_failed",
    "root_missing": f"{FOREIGN}::test_a_blocked_state_is_announced_once",
    "master_key_unreadable": f"{OPS_EVENTS}::test_unreadable_keyring_stops_the_start_and_publishes_secret_undecryptable",
    "restored_behind": f"{OPS_EVENTS}::test_reconcile_drift_and_restored_behind_create_notifications",
    "foreign_writer": f"{FOREIGN}::test_foreign_file_in_managed_dir_pauses_series",
    "job_dead_lettered": f"{OPS_EVENTS}::test_retries_exhausted_sets_dead_letter_and_emits_job_dead_lettered",
    "tombstone_key_unknown": f"{KEYRING}::test_lost_keyring_tombstone_journal_inert",
    "tombstone_root_pending": "tests/integration/test_restore_epoch.py::test_replay_tries_known_root_ids_and_reports_root_mismatch",
    "tombstone_sentinel_forged": "tests/property/test_tombstone_scopes.py::test_forged_tombstone_line_never_deletes",
    "dns_blocked": "tests/unit/test_fetcher.py::test_dns_sinkhole_reported_as_dns_blocked",
    "abrupt_stop": f"{OPS_EVENTS}::test_reconcile_drift_and_restored_behind_create_notifications",
}


# The M1b rows whose emitter has shipped: the real test that plays them (L18, L21).
_CLOSED = {
    "suwayomi_reinstalled": "tests/integration/test_suwayomi_reinstall.py::test_connected_service_reinstall_pauses_without_cascade",
    "kavita_asleep_sablier": "tests/integration/test_wake_kavita.py::test_kavita_asleep_wake_timeout_defers_without_unreachable_or_write",
}


def _proved(row: FailureRow) -> FailureRow:
    if row.key in SIMULATED:
        return replace(row, simulated=True)
    if row.key in _CLOSED:
        return replace(row, proof=_CLOSED[row.key])
    if row.milestone != "M1a":
        return replace(row, proof=f"lot {row.milestone}: the emitter ships with it")
    return replace(row, proof=_REAL_TESTS[row.key])  # a missing key is a row without proof


FAILURE_MATRIX: tuple[FailureRow, ...] = tuple(_proved(row) for row in _ROWS)


def rows_for(milestone: str) -> tuple[FailureRow, ...]:
    return tuple(row for row in FAILURE_MATRIX if row.milestone == milestone)
