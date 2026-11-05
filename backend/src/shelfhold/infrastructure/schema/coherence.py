"""Coherence tables: intents, locks, export plans, secrets and tombstones (02 sections 3.3, 5.9)."""

import sqlalchemy as sa

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    add_trigger,
    in_check,
    metadata,
    now_column,
    positive_check,
    prefixed_hash_check,
    ulid_check,
    ulid_column,
)

INTENT_OPS = (
    "rename", "upgrade", "recycle", "delete", "purge_upstream", "safeguard", "rmdir",
    "copy_cross_root", "unlink", "tombstone_rekey", "write",
)  # fmt: skip
# `stale_from_backup`: an intention that came back with a restored snapshot; never replayed
# (EF-INT-026).
INTENT_STATES = ("pending", "applying", "done", "cancelled", "stale_from_backup")
PLAN_KINDS = ("export", "adopt", "rename_plan", "detach", "remove")
PLAN_STATUSES = ("planned", "applying", "done", "cancelled", "failed", "stale")
PLAN_OPS = (
    "mkdir", "write", "rename", "recycle", "delete", "touch_mtime", "api_call", "register",
)  # fmt: skip
PLAN_OP_STATUSES = ("pending", "done", "skipped", "failed")
KEY_STATES = ("active", "retired", "compromised")
TOMBSTONE_SCOPES = ("variant", "place", "series_binding", "work")
TOMBSTONE_ALGORITHM = "xchacha20poly1305"
TOMBSTONE_NONCE_BYTES = 24

# Written before the operation, validated in the database (EF-EXP-041, ADR-0031). The format is
# frozen and versioned: a build that finds an unknown version refuses to replay (ENF-OPS-019).
fs_intent = sa.Table(
    "fs_intent",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("op", sa.String(), nullable=False),
    sa.Column("intent_schema_version", sa.String(), nullable=False),
    sa.Column("src_path", sa.Text(), nullable=True),
    sa.Column("dst_path", sa.Text(), nullable=True),
    sa.Column("src_root", sa.String(26), sa.ForeignKey("physical_root.root_id")),
    sa.Column("dst_root", sa.String(26), sa.ForeignKey("physical_root.root_id")),
    sa.Column("expected_sha256", sa.String(), nullable=True),
    sa.Column(
        "expected_file_id", sa.JSON(), nullable=True
    ),  # ino, size, mtime (dev: same root only)
    sa.Column("plan_id", sa.String(26), sa.ForeignKey("export_plan.plan_id", ondelete="SET NULL")),
    sa.Column("state", sa.String(), nullable=False, server_default="pending"),
    sa.Column("epoch", sa.Integer(), nullable=False),  # instance.restore_epoch when written
    now_column(),
    sa.Column("resolved_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    prefixed_hash_check("expected_sha256"),
    in_check("op", INTENT_OPS),
    in_check("state", INTENT_STATES),
    positive_check("epoch"),
    sa.Index("ix_fs_intent_state", "state"),
    sa.Index("ix_fs_intent_plan", "plan_id"),
)

resource_lock = sa.Table(
    "resource_lock",
    metadata,
    sa.Column("resource", sa.String(), primary_key=True),  # work:<id> or path:<destination>/<dir>
    ulid_column("holder_job_id"),
    sa.Column("lease_until", UtcDateTime(), nullable=False),
    now_column("acquired_at"),
    ulid_check("holder_job_id"),
)

# `base_version` fingerprints what the plan was computed from; a plan whose base moved is stale
# (EF-EXP-042). A plan outlives its destination, which is then unset.
export_plan = sa.Table(
    "export_plan",
    metadata,
    ulid_column("plan_id", primary_key=True),
    sa.Column(
        "shelf_destination_id",
        sa.String(26),
        sa.ForeignKey("shelf_destination.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("kind", sa.String(), nullable=False),
    sa.Column("dry_run", sa.Boolean(), nullable=False),
    sa.Column("base_version", sa.String(), nullable=False),  # 128-bit digest, hex (02 section 6.4)
    sa.Column("files_fingerprint", sa.String(), nullable=True),
    sa.Column("batches", sa.JSON(), nullable=False, server_default="[]"),
    sa.Column("status", sa.String(), nullable=False, server_default="planned"),
    now_column(),
    sa.Column("updated_at", UtcDateTime(), nullable=True),
    ulid_check("plan_id"),
    ulid_check("shelf_destination_id"),
    sa.CheckConstraint(
        "length(base_version) BETWEEN 1 AND 64 AND base_version NOT GLOB '*[^0-9a-f]*'",
        name="base_version_hex",
    ),
    prefixed_hash_check("files_fingerprint"),
    in_check("kind", PLAN_KINDS),
    in_check("status", PLAN_STATUSES),
)

export_op = sa.Table(
    "export_op",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "plan_id",
        sa.String(26),
        sa.ForeignKey("export_plan.plan_id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("batch", sa.Integer(), nullable=False),
    sa.Column("position", sa.Integer(), nullable=False),
    sa.Column("op", sa.String(), nullable=False),
    sa.Column("src_path", sa.Text(), nullable=True),
    sa.Column("dst_path", sa.Text(), nullable=True),
    sa.Column("payload", sa.JSON(), nullable=True),
    sa.Column("status", sa.String(), nullable=False, server_default="pending"),
    sa.Column("error_code", sa.String(), nullable=True),
    ulid_check("id"),
    in_check("op", PLAN_OPS),
    in_check("status", PLAN_OP_STATUSES),
    positive_check("batch"),
    positive_check("position"),
    sa.UniqueConstraint("plan_id", "batch", "position"),
)

# Keyring registry (ENF-SEC-005). The key material lives in the keyring file, never here.
secret_key = sa.Table(
    "secret_key",
    metadata,
    sa.Column("kid", sa.String(), primary_key=True),
    sa.Column("state", sa.String(), nullable=False),
    sa.Column("alg", sa.String(), nullable=False, server_default=TOMBSTONE_ALGORITHM),
    now_column(),
    sa.Column("retired_at", UtcDateTime(), nullable=True),
    in_check("state", KEY_STATES),
    sa.CheckConstraint(
        '"state" <> \'retired\' OR "retired_at" IS NOT NULL', name="retired_has_date"
    ),
)

# Registry of every `kid` ever used for a tombstone: the only source for "never retired",
# including for unmounted roots (EF-BIB-053, GAP-13-01). Deliberately independent of the keyring.
tombstone_kid = sa.Table(
    "tombstone_kid",
    metadata,
    sa.Column("kid", sa.String(), primary_key=True),
    sa.Column("first_seq", sa.Integer(), nullable=False),
    now_column(),
)

# Encrypted raw identifiers only: no digest, no salt, no title (EF-BIB-054, I46). The cleartext
# is never stored; `reason` is opaque.
content_tombstone = sa.Table(
    "content_tombstone",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("v", sa.Integer(), nullable=False),  # format version, part of the AAD (EF-BIB-054)
    sa.Column("scope", sa.String(), nullable=False),
    sa.Column("seq", sa.Integer(), nullable=False),
    sa.Column("kid", sa.String(), sa.ForeignKey("tombstone_kid.kid"), nullable=False),
    sa.Column("alg", sa.String(), nullable=False),
    sa.Column("nonce", sa.LargeBinary(), nullable=False),
    sa.Column("ct", sa.LargeBinary(), nullable=False),
    now_column(),
    sa.Column("epoch", sa.Integer(), nullable=False),
    sa.Column("reason", sa.String(), nullable=True),
    ulid_check("id"),
    in_check("scope", TOMBSTONE_SCOPES),
    sa.CheckConstraint(f"\"alg\" = '{TOMBSTONE_ALGORITHM}'", name="alg_known"),
    sa.CheckConstraint(f'length("nonce") = {TOMBSTONE_NONCE_BYTES}', name="nonce_length"),
    positive_check("v", minimum=1),
    positive_check("seq", minimum=1),
    sa.CheckConstraint('length("reason") <= 64', name="reason_length"),
    positive_check("epoch"),
    sa.UniqueConstraint("seq"),
)

# A journal line meant for a read-write root that was absent during a purge; written first
# when the root comes back (EF-BIB-052, GAP-12-14).
tombstone_pending_root = sa.Table(
    "tombstone_pending_root",
    metadata,
    sa.Column("root_id", sa.String(26), sa.ForeignKey("physical_root.root_id"), nullable=False),
    sa.Column("seq", sa.Integer(), nullable=False),
    sa.Column("line", sa.Text(), nullable=False),  # the encrypted JSON line, as it will be written
    now_column(),
    sa.PrimaryKeyConstraint("root_id", "seq"),
)

# The "lost keyring" waiver (EF-BIB-054 (5), R62): the registered kids that were absent from the
# keyring when the admin typed the waiver. Lines under them are inert from then on (they neither
# block nor delete); before it they block. Append-only: a waiver is never taken back by a write,
# only by importing the key again, which makes the lines decrypt (and block) anyway.
tombstone_lost_kid = sa.Table(
    "tombstone_lost_kid",
    metadata,
    sa.Column("kid", sa.String(), sa.ForeignKey("tombstone_kid.kid"), primary_key=True),
    sa.Column("actor_id", sa.String(), nullable=True),
    now_column("accepted_at"),
)

# The registry only grows (GAP-13-01), and a key cited by it is never removed from the registry
# of keys (ENF-SEC-005, "a cited kid is never retired").
add_trigger(
    tombstone_kid,
    "tombstone_kid_no_update",
    "BEFORE UPDATE ON tombstone_kid BEGIN SELECT RAISE(ABORT, 'tombstone_kid is append-only'); END",
)
add_trigger(
    tombstone_kid,
    "tombstone_kid_no_delete",
    "BEFORE DELETE ON tombstone_kid BEGIN SELECT RAISE(ABORT, 'tombstone_kid is append-only'); END",
)
add_trigger(
    secret_key,
    "secret_key_cited_kid_kept",
    "BEFORE DELETE ON secret_key "
    "WHEN EXISTS (SELECT 1 FROM tombstone_kid WHERE kid = OLD.kid) "
    "BEGIN SELECT RAISE(ABORT, 'kid is cited by a tombstone'); END",
)
add_trigger(
    tombstone_lost_kid,
    "tombstone_lost_kid_no_update",
    "BEFORE UPDATE ON tombstone_lost_kid "
    "BEGIN SELECT RAISE(ABORT, 'tombstone_lost_kid is append-only'); END",
)
add_trigger(
    tombstone_lost_kid,
    "tombstone_lost_kid_no_delete",
    "BEFORE DELETE ON tombstone_lost_kid "
    "BEGIN SELECT RAISE(ABORT, 'tombstone_lost_kid is append-only'); END",
)
