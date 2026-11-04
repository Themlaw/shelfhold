"""Platform tables: settings, instance, physical roots, jobs, schedule, events (02 section 3.5)."""

import sqlalchemy as sa

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    add_trigger,
    bool_column,
    in_check,
    metadata,
    now_column,
    positive_check,
    ulid_check,
    ulid_column,
)

REFRESH_MODES = ("piloted", "always_on")
ROOT_MODES = ("rw", "adopt_ro")  # PathMap mode: `adopt_ro` is never written (EF-EXP-060)
SENTINEL_STATES = ("ok", "missing", "mismatch", "stale")
JOB_STATUSES = ("queued", "running", "succeeded", "failed", "cancelled", "dead_letter", "paused")
PRIORITY_CLASSES = ("new", "request", "backfill")
EVENT_VISIBILITIES = ("private", "household", "ops")

# Migration 0001, kept as is: the lowest configuration layer (ENF-OPS-004).
app_setting = sa.Table(
    "app_setting",
    metadata,
    sa.Column("key", sa.String(), primary_key=True),
    sa.Column("value", sa.String(), nullable=False),
)

# One row. `restore_epoch` / `restore_generation` are set by any restore or rollback (EF-INT-026).
instance = sa.Table(
    "instance",
    metadata,
    ulid_column("instance_id", primary_key=True),
    sa.Column("singleton", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("jurisdiction", sa.String(8), nullable=False),
    sa.Column("refresh_mode", sa.String(), nullable=False, server_default="piloted"),
    sa.Column("lease_heartbeat_at", UtcDateTime(), nullable=True),
    sa.Column("restore_epoch", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("restore_epoch_at", UtcDateTime(), nullable=True),
    sa.Column("restore_generation", sa.Integer(), nullable=False, server_default="0"),
    # Truncation detection of the tombstone journal (EF-BIB-054, 02 section 3.12; used from M1b).
    sa.Column("tombstone_count", sa.Integer(), nullable=True),
    sa.Column("tombstone_last_tag", sa.String(), nullable=True),
    now_column(),
    ulid_check("instance_id"),
    sa.CheckConstraint("singleton = 1", name="single_row"),
    in_check("refresh_mode", REFRESH_MODES),
    positive_check("restore_epoch"),
    positive_check("restore_generation"),
    sa.UniqueConstraint("singleton"),
)

# One row per physical root: an entry of the path map, with a stable identity of its own.
physical_root = sa.Table(
    "physical_root",
    metadata,
    ulid_column("root_id", primary_key=True),
    sa.Column("path_entry", sa.String(), nullable=False),
    sa.Column("mode", sa.String(), nullable=False, server_default="rw"),
    sa.Column("st_dev", sa.BigInteger(), nullable=True),
    sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("predecessor_instance_ids", sa.JSON(), nullable=False, server_default="[]"),
    now_column(),
    ulid_check("root_id"),
    in_check("mode", ROOT_MODES),
    positive_check("generation"),
    sa.UniqueConstraint("path_entry"),
)

# What the `.shelfhold-mount-id` file of a root is expected to say, and what was last seen.
mount_sentinel = sa.Table(
    "mount_sentinel",
    metadata,
    sa.Column(
        "root_id",
        sa.String(26),
        sa.ForeignKey("physical_root.root_id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("st_dev", sa.BigInteger(), nullable=False),
    sa.Column("capabilities", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("mount_id", sa.String(), nullable=False),
    sa.Column("generation", sa.Integer(), nullable=False),
    sa.Column("checked_at", UtcDateTime(), nullable=False),
    sa.Column("state", sa.String(), nullable=False),
    sa.Column("tombstone_count", sa.Integer(), nullable=True),
    sa.Column("tombstone_last_tag", sa.String(), nullable=True),
    in_check("state", SENTINEL_STATES),
    positive_check("generation"),
)

# Periodic work. Huey catches nothing up by itself: one minute-by-minute scan reads this table.
schedule = sa.Table(
    "schedule",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("job_kind", sa.String(), nullable=False),
    sa.Column(
        "scope_key", sa.String(), nullable=False, server_default=sa.text("''")
    ),  # e.g. one per source
    sa.Column("cron", sa.String(), nullable=False),
    sa.Column("jitter_s", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("window", sa.JSON(), nullable=True),  # UTC bounds, rendered local by the interface
    bool_column("enabled", default=True),
    sa.Column("last_run_at", UtcDateTime(), nullable=True),
    sa.Column("next_due_at", UtcDateTime(), nullable=True),
    now_column(),
    ulid_check("id"),
    positive_check("jitter_s"),
    sa.UniqueConstraint("job_kind", "scope_key"),
    sa.Index("ix_schedule_due", "enabled", "next_due_at"),
)

# Mirror of the Huey queue for the Activity view; the idempotency unit is the chapter.
job_view = sa.Table(
    "job_view",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("huey_task_id", sa.String(), nullable=True),
    sa.Column("kind", sa.String(), nullable=False),
    sa.Column("status", sa.String(), nullable=False),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("progress", sa.Float(), nullable=True),
    sa.Column("dedupe_key", sa.String(), nullable=True),
    sa.Column("source_key", sa.String(), nullable=True),
    sa.Column("limit_key", sa.String(), nullable=True),
    sa.Column("last_error", sa.Text(), nullable=True),
    sa.Column("error_code", sa.String(), nullable=True),
    sa.Column("priority_class", sa.String(), nullable=False, server_default="new"),
    ulid_column("request_id", nullable=True),
    now_column(),
    sa.Column("started_at", UtcDateTime(), nullable=True),
    sa.Column("finished_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    ulid_check("request_id"),
    in_check("status", JOB_STATUSES),
    in_check("priority_class", PRIORITY_CLASSES),
    positive_check("attempts"),
    sa.CheckConstraint(
        "progress IS NULL OR (progress >= 0 AND progress <= 1)", name="progress_range"
    ),
    sa.UniqueConstraint("huey_task_id"),
    sa.Index("ix_job_view_status", "status", "created_at"),
    sa.Index(
        "uq_job_view_dedupe_active",
        "dedupe_key",
        unique=True,
        sqlite_where=sa.text(
            "dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'paused')"
        ),
    ),
)

# Concurrency limit per upstream source or domain; leases are released at start-up (ENF-OPS-007).
domain_slot = sa.Table(
    "domain_slot",
    metadata,
    sa.Column("limit_key", sa.String(), primary_key=True),
    sa.Column("slots", sa.Integer(), nullable=False),
    sa.Column("in_use", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("lease_until", UtcDateTime(), nullable=True),
    positive_check("slots", minimum=1),
    sa.CheckConstraint("in_use >= 0 AND in_use <= slots", name="in_use_range"),
)

# Append-only. The id is the SSE id: a monotone sequence, never reused (AUTOINCREMENT).
event = sa.Table(
    "event",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
    sa.Column("type", sa.String(), nullable=False),
    sa.Column("schema_version", sa.Integer(), nullable=False),
    sa.Column("payload", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("occurred_at", UtcDateTime(), nullable=False),
    sa.Column("actor_id", sa.String(), nullable=True),
    sa.Column("visibility", sa.String(), nullable=False),
    positive_check("schema_version", minimum=1),
    in_check("visibility", EVENT_VISIBILITIES),
    sa.Index("ix_event_type", "type", "id"),
    sa.Index("ix_event_occurred_at", "occurred_at"),
    sqlite_autoincrement=True,
)

# Written in the same transaction as its event, sent after commit, purged on success.
notification_pending = sa.Table(
    "notification_pending",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "event_id", sa.Integer(), sa.ForeignKey("event.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("channel", sa.String(), nullable=False),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("next_try_at", UtcDateTime(), nullable=False),
    now_column(),
    ulid_check("id"),
    positive_check("attempts"),
    sa.Index("ix_notification_pending_due", "next_try_at"),
    sa.Index("ix_notification_pending_event", "event_id"),
)

add_trigger(
    event,
    "event_append_only",
    "BEFORE UPDATE ON event BEGIN SELECT RAISE(ABORT, 'event is append-only'); END",
)  # DELETE stays allowed: the 90-day purge (ENF-OPS-009)
