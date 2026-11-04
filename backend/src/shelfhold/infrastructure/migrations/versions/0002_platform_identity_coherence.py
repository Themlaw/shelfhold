"""M1a schema, platform / identity / coherence (PLAN-M1a L02).

Tables only: catalog and acquisition arrive in 0003, storage / export / reading in 0004.
The revision is a frozen snapshot: it does not import the live metadata, and the parity test
`test_metadata_matches_migrations` keeps the two honest.
"""

# ruff: noqa: E501  (frozen, generated constraint expressions)

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

TABLES = (
    "tombstone_pending_root",
    "session",
    "notification_pending",
    "mount_sentinel",
    "fs_intent",
    "export_op",
    "content_tombstone",
    "api_key",
    "user",
    "tombstone_kid",
    "secret_key",
    "schedule",
    "resource_lock",
    "physical_root",
    "job_view",
    "instance",
    "idempotency_key",
    "export_plan",
    "event",
    "domain_slot",
    "audit",
)


def upgrade() -> None:
    op.create_table(
        "audit",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("action", sa.String(), nullable=False),
        sa.Column("target", sa.String(), nullable=True),
        sa.Column("at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_audit_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit")),
    )
    op.create_index("ix_audit_actor", "audit", ["actor_id", "at"], unique=False)
    op.create_index("ix_audit_at", "audit", ["at"], unique=False)
    op.create_table(
        "domain_slot",
        sa.Column("limit_key", sa.String(), nullable=False),
        sa.Column("slots", sa.Integer(), nullable=False),
        sa.Column("in_use", sa.Integer(), server_default="0", nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=True),
        sa.CheckConstraint('"slots" >= 1', name=op.f("ck_domain_slot_slots_min")),
        sa.CheckConstraint(
            "in_use >= 0 AND in_use <= slots", name=op.f("ck_domain_slot_in_use_range")
        ),
        sa.PrimaryKeyConstraint("limit_key", name=op.f("pk_domain_slot")),
    )
    op.create_table(
        "event",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column("visibility", sa.String(), nullable=False),
        sa.CheckConstraint('"schema_version" >= 1', name=op.f("ck_event_schema_version_min")),
        sa.CheckConstraint(
            "\"visibility\" IN ('private', 'household', 'ops')",
            name=op.f("ck_event_visibility_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_event")),
        sqlite_autoincrement=True,
    )
    op.create_index("ix_event_occurred_at", "event", ["occurred_at"], unique=False)
    op.create_index("ix_event_type", "event", ["type", "id"], unique=False)
    op.create_table(
        "export_plan",
        sa.Column("plan_id", sa.String(length=26), nullable=False),
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("dry_run", sa.Boolean(), nullable=False),
        sa.Column("base_version", sa.String(), nullable=False),
        sa.Column("files_fingerprint", sa.String(), nullable=True),
        sa.Column("batches", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("status", sa.String(), server_default="planned", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "\"kind\" IN ('export', 'adopt', 'rename_plan', 'detach', 'remove')",
            name=op.f("ck_export_plan_kind_values"),
        ),
        sa.CheckConstraint(
            "\"status\" IN ('planned', 'applying', 'done', 'cancelled', 'failed', 'stale')",
            name=op.f("ck_export_plan_status_values"),
        ),
        sa.CheckConstraint(
            'length("base_version") = 71 AND "base_version" GLOB \'sha256:*\' AND substr("base_version", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_export_plan_base_version_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("files_fingerprint") = 71 AND "files_fingerprint" GLOB \'sha256:*\' AND substr("files_fingerprint", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_export_plan_files_fingerprint_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("plan_id") = 26 AND "plan_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_export_plan_plan_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("shelf_destination_id") = 26 AND "shelf_destination_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_export_plan_shelf_destination_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("plan_id", name=op.f("pk_export_plan")),
    )
    op.create_table(
        "idempotency_key",
        sa.Column("actor_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("method", sa.String(), nullable=False),
        sa.Column("path", sa.String(), nullable=False),
        sa.Column("request_hash", sa.String(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.Column("response_body", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            'length("request_hash") = 71 AND "request_hash" GLOB \'sha256:*\' AND substr("request_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_idempotency_key_request_hash_sha256_prefixed"),
        ),
        sa.PrimaryKeyConstraint("actor_id", "key", name=op.f("pk_idempotency_key")),
    )
    op.create_index("ix_idempotency_key_expiry", "idempotency_key", ["expires_at"], unique=False)
    op.create_table(
        "instance",
        sa.Column("instance_id", sa.String(length=26), nullable=False),
        sa.Column("singleton", sa.Integer(), server_default="1", nullable=False),
        sa.Column("jurisdiction", sa.String(length=8), nullable=False),
        sa.Column("refresh_mode", sa.String(), server_default="piloted", nullable=False),
        sa.Column("lease_heartbeat_at", sa.DateTime(), nullable=True),
        sa.Column("restore_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("restore_epoch_at", sa.DateTime(), nullable=True),
        sa.Column("restore_generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "\"refresh_mode\" IN ('piloted', 'always_on')",
            name=op.f("ck_instance_refresh_mode_values"),
        ),
        sa.CheckConstraint('"restore_epoch" >= 0', name=op.f("ck_instance_restore_epoch_min")),
        sa.CheckConstraint(
            '"restore_generation" >= 0', name=op.f("ck_instance_restore_generation_min")
        ),
        sa.CheckConstraint(
            'length("instance_id") = 26 AND "instance_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_instance_instance_id_ulid"),
        ),
        sa.CheckConstraint("singleton = 1", name=op.f("ck_instance_single_row")),
        sa.PrimaryKeyConstraint("instance_id", name=op.f("pk_instance")),
        sa.UniqueConstraint("singleton", name=op.f("uq_instance_singleton")),
    )
    op.create_table(
        "job_view",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("huey_task_id", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("progress", sa.Float(), nullable=True),
        sa.Column("dedupe_key", sa.String(), nullable=True),
        sa.Column("source_key", sa.String(), nullable=True),
        sa.Column("limit_key", sa.String(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column("priority_class", sa.String(), server_default="new", nullable=False),
        sa.Column("request_id", sa.String(length=26), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint('"attempts" >= 0', name=op.f("ck_job_view_attempts_min")),
        sa.CheckConstraint(
            "\"priority_class\" IN ('new', 'request', 'backfill')",
            name=op.f("ck_job_view_priority_class_values"),
        ),
        sa.CheckConstraint(
            "\"status\" IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead_letter', 'paused')",
            name=op.f("ck_job_view_status_values"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_job_view_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("request_id") = 26 AND "request_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_job_view_request_id_ulid"),
        ),
        sa.CheckConstraint(
            "progress IS NULL OR (progress >= 0 AND progress <= 1)",
            name=op.f("ck_job_view_progress_range"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_job_view")),
        sa.UniqueConstraint("huey_task_id", name=op.f("uq_job_view_huey_task_id")),
    )
    op.create_index("ix_job_view_status", "job_view", ["status", "created_at"], unique=False)
    op.create_index(
        "uq_job_view_dedupe_active",
        "job_view",
        ["dedupe_key"],
        unique=True,
        sqlite_where=sa.text(
            "dedupe_key IS NOT NULL AND status IN ('queued', 'running', 'paused')"
        ),
    )
    op.create_table(
        "physical_root",
        sa.Column("root_id", sa.String(length=26), nullable=False),
        sa.Column("path_entry", sa.String(), nullable=False),
        sa.Column("st_dev", sa.BigInteger(), nullable=True),
        sa.Column("generation", sa.Integer(), server_default="0", nullable=False),
        sa.Column("predecessor_instance_ids", sa.JSON(), server_default="[]", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint('"generation" >= 0', name=op.f("ck_physical_root_generation_min")),
        sa.CheckConstraint(
            'length("root_id") = 26 AND "root_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_physical_root_root_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("root_id", name=op.f("pk_physical_root")),
        sa.UniqueConstraint("path_entry", name=op.f("uq_physical_root_path_entry")),
    )
    op.create_table(
        "resource_lock",
        sa.Column("resource", sa.String(), nullable=False),
        sa.Column("holder_job_id", sa.String(length=26), nullable=False),
        sa.Column("lease_until", sa.DateTime(), nullable=False),
        sa.Column(
            "acquired_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            'length("holder_job_id") = 26 AND "holder_job_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_resource_lock_holder_job_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("resource", name=op.f("pk_resource_lock")),
    )
    op.create_table(
        "schedule",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("job_kind", sa.String(), nullable=False),
        sa.Column("cron", sa.String(), nullable=False),
        sa.Column("jitter_s", sa.Integer(), server_default="0", nullable=False),
        sa.Column("window", sa.JSON(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("last_run_at", sa.DateTime(), nullable=True),
        sa.Column("next_due_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint('"jitter_s" >= 0', name=op.f("ck_schedule_jitter_s_min")),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_schedule_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_schedule")),
        sa.UniqueConstraint("job_kind", name=op.f("uq_schedule_job_kind")),
    )
    op.create_index("ix_schedule_due", "schedule", ["enabled", "next_due_at"], unique=False)
    op.create_table(
        "secret_key",
        sa.Column("kid", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("alg", sa.String(), server_default="xchacha20poly1305", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("retired_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            '"state" <> \'retired\' OR "retired_at" IS NOT NULL',
            name=op.f("ck_secret_key_retired_has_date"),
        ),
        sa.CheckConstraint(
            "\"state\" IN ('active', 'retired', 'compromised')",
            name=op.f("ck_secret_key_state_values"),
        ),
        sa.PrimaryKeyConstraint("kid", name=op.f("pk_secret_key")),
    )
    op.create_table(
        "tombstone_kid",
        sa.Column("kid", sa.String(), nullable=False),
        sa.Column("first_seq", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.PrimaryKeyConstraint("kid", name=op.f("pk_tombstone_kid")),
    )
    op.create_table(
        "user",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("username", sa.String(collation="NOCASE"), nullable=False),
        sa.Column("password_hash", sa.String(), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("can_rate", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("locale", sa.String(), server_default="fr", nullable=False),
        sa.Column("timezone", sa.String(), server_default="UTC", nullable=False),
        sa.Column("title_lang_pref", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("theme", sa.String(), nullable=True),
        sa.Column("color_scheme", sa.String(), server_default="auto", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "\"color_scheme\" IN ('auto', 'light', 'dark')",
            name=op.f("ck_user_color_scheme_values"),
        ),
        sa.CheckConstraint(
            "\"password_hash\" GLOB '$argon2id$*'", name=op.f("ck_user_password_hash_argon2id")
        ),
        sa.CheckConstraint(
            '"role" <> \'admin\' OR "can_rate"', name=op.f("ck_user_admin_can_rate")
        ),
        sa.CheckConstraint(
            "\"role\" IN ('admin', 'member', 'child')", name=op.f("ck_user_role_values")
        ),
        sa.CheckConstraint(
            "\"theme\" IN ('bibliotheque', 'console', 'galerie', 'calme')",
            name=op.f("ck_user_theme_values"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_user_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user")),
        sa.UniqueConstraint("username", name=op.f("uq_user_username")),
    )
    op.create_table(
        "api_key",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("prefix", sa.String(), nullable=False),
        sa.Column("hash", sa.String(), nullable=False),
        sa.Column("scopes", sa.JSON(), nullable=False),
        sa.Column("read_only", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=True),
        sa.Column("last_used_at", sa.DateTime(), nullable=True),
        sa.Column("rotated_from", sa.String(length=26), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.Column("subject_user_id", sa.String(length=26), nullable=True),
        sa.Column("issued_by", sa.String(length=26), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("json_type(\"scopes\") = 'array'", name=op.f("ck_api_key_scopes_array")),
        sa.CheckConstraint(
            'length("hash") = 71 AND "hash" GLOB \'sha256:*\' AND substr("hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_api_key_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_api_key_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["issued_by"], ["user.id"], name=op.f("fk_api_key_issued_by_user"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["rotated_from"],
            ["api_key.id"],
            name=op.f("fk_api_key_rotated_from_api_key"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["subject_user_id"],
            ["user.id"],
            name=op.f("fk_api_key_subject_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name=op.f("fk_api_key_user_id_user"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_api_key")),
        sa.UniqueConstraint("prefix", name=op.f("uq_api_key_prefix")),
    )
    op.create_index("ix_api_key_user", "api_key", ["user_id"], unique=False)
    op.create_table(
        "content_tombstone",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("kid", sa.String(), nullable=False),
        sa.Column("alg", sa.String(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("ct", sa.LargeBinary(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.CheckConstraint(
            "\"alg\" = 'xchacha20poly1305'", name=op.f("ck_content_tombstone_alg_known")
        ),
        sa.CheckConstraint('"epoch" >= 0', name=op.f("ck_content_tombstone_epoch_min")),
        sa.CheckConstraint(
            "\"scope\" IN ('variant', 'place', 'series_binding', 'work')",
            name=op.f("ck_content_tombstone_scope_values"),
        ),
        sa.CheckConstraint('"seq" >= 1', name=op.f("ck_content_tombstone_seq_min")),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_content_tombstone_id_ulid"),
        ),
        sa.CheckConstraint('length("nonce") = 24', name=op.f("ck_content_tombstone_nonce_length")),
        sa.ForeignKeyConstraint(
            ["kid"], ["tombstone_kid.kid"], name=op.f("fk_content_tombstone_kid_tombstone_kid")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content_tombstone")),
        sa.UniqueConstraint("seq", name=op.f("uq_content_tombstone_seq")),
    )
    op.create_table(
        "export_op",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("plan_id", sa.String(length=26), nullable=False),
        sa.Column("batch", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("op", sa.String(), nullable=False),
        sa.Column("src_path", sa.Text(), nullable=True),
        sa.Column("dst_path", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.CheckConstraint('"batch" >= 0', name=op.f("ck_export_op_batch_min")),
        sa.CheckConstraint(
            "\"op\" IN ('mkdir', 'write', 'rename', 'recycle', 'delete', 'touch_mtime', 'api_call')",
            name=op.f("ck_export_op_op_values"),
        ),
        sa.CheckConstraint('"position" >= 0', name=op.f("ck_export_op_position_min")),
        sa.CheckConstraint(
            "\"status\" IN ('pending', 'done', 'skipped', 'failed')",
            name=op.f("ck_export_op_status_values"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_export_op_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["export_plan.plan_id"],
            name=op.f("fk_export_op_plan_id_export_plan"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_op")),
        sa.UniqueConstraint(
            "plan_id", "batch", "position", name=op.f("uq_export_op_plan_id_batch_position")
        ),
    )
    op.create_table(
        "fs_intent",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("op", sa.String(), nullable=False),
        sa.Column("intent_schema_version", sa.String(), nullable=False),
        sa.Column("src_path", sa.Text(), nullable=True),
        sa.Column("dst_path", sa.Text(), nullable=True),
        sa.Column("src_root", sa.String(length=26), nullable=True),
        sa.Column("dst_root", sa.String(length=26), nullable=True),
        sa.Column("expected_sha256", sa.String(), nullable=True),
        sa.Column("expected_file_id", sa.JSON(), nullable=True),
        sa.Column("plan_id", sa.String(length=26), nullable=True),
        sa.Column("state", sa.String(), server_default="pending", nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint('"epoch" >= 0', name=op.f("ck_fs_intent_epoch_min")),
        sa.CheckConstraint(
            "\"op\" IN ('rename', 'upgrade', 'recycle', 'delete', 'purge_upstream', 'safeguard', 'rmdir', 'copy_cross_root', 'unlink', 'tombstone_rekey')",
            name=op.f("ck_fs_intent_op_values"),
        ),
        sa.CheckConstraint(
            "\"state\" IN ('pending', 'applying', 'done', 'cancelled', 'stale_from_backup')",
            name=op.f("ck_fs_intent_state_values"),
        ),
        sa.CheckConstraint(
            'length("expected_sha256") = 71 AND "expected_sha256" GLOB \'sha256:*\' AND substr("expected_sha256", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_fs_intent_expected_sha256_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_fs_intent_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["dst_root"],
            ["physical_root.root_id"],
            name=op.f("fk_fs_intent_dst_root_physical_root"),
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["export_plan.plan_id"],
            name=op.f("fk_fs_intent_plan_id_export_plan"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["src_root"],
            ["physical_root.root_id"],
            name=op.f("fk_fs_intent_src_root_physical_root"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_fs_intent")),
    )
    op.create_index("ix_fs_intent_state", "fs_intent", ["state"], unique=False)
    op.create_table(
        "mount_sentinel",
        sa.Column("root_id", sa.String(length=26), nullable=False),
        sa.Column("st_dev", sa.BigInteger(), nullable=False),
        sa.Column("capabilities", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("mount_id", sa.String(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("checked_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.CheckConstraint('"generation" >= 0', name=op.f("ck_mount_sentinel_generation_min")),
        sa.CheckConstraint(
            "\"state\" IN ('ok', 'missing', 'mismatch', 'stale')",
            name=op.f("ck_mount_sentinel_state_values"),
        ),
        sa.ForeignKeyConstraint(
            ["root_id"],
            ["physical_root.root_id"],
            name=op.f("fk_mount_sentinel_root_id_physical_root"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("root_id", name=op.f("pk_mount_sentinel")),
    )
    op.create_table(
        "notification_pending",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("next_try_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint('"attempts" >= 0', name=op.f("ck_notification_pending_attempts_min")),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_notification_pending_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["event.id"],
            name=op.f("fk_notification_pending_event_id_event"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_notification_pending")),
    )
    op.create_index(
        "ix_notification_pending_due", "notification_pending", ["next_try_at"], unique=False
    )
    op.create_table(
        "session",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("token_hash", sa.String(), nullable=False),
        sa.Column("csrf_secret", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
        sa.Column("auth_time", sa.DateTime(), nullable=False),
        sa.Column("absolute_expiry", sa.DateTime(), nullable=False),
        sa.Column("remember", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("user_agent_label", sa.String(), nullable=True),
        sa.Column("ip", sa.String(), nullable=True),
        sa.Column("revoked_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_session_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("token_hash") = 71 AND "token_hash" GLOB \'sha256:*\' AND substr("token_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_session_token_hash_sha256_prefixed"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["user.id"], name=op.f("fk_session_user_id_user"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_session_token_hash")),
    )
    op.create_index("ix_session_expiry", "session", ["absolute_expiry"], unique=False)
    op.create_index("ix_session_user", "session", ["user_id"], unique=False)
    op.create_table(
        "tombstone_pending_root",
        sa.Column("root_id", sa.String(length=26), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("line", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["root_id"],
            ["physical_root.root_id"],
            name=op.f("fk_tombstone_pending_root_root_id_physical_root"),
        ),
        sa.PrimaryKeyConstraint("root_id", "seq", name=op.f("pk_tombstone_pending_root")),
    )
    # Append-only event log (EF-INT-002). DELETE stays allowed for the 90-day purge (ENF-OPS-009).
    op.execute(
        "CREATE TRIGGER event_append_only BEFORE UPDATE ON event "
        "BEGIN SELECT RAISE(ABORT, 'event is append-only'); END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS event_append_only")
    for table in TABLES:  # children first; indexes go with their table
        op.drop_table(table)
