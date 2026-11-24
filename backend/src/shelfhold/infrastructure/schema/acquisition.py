"""Acquisition tables: sources, bindings, follows, download tickets, plugins (02 sections 3.1, 3.4).

Decision A1 (PLAN-M1a): the table keeps the name `source_binding`, with `edition_id NOT NULL`,
a `priority` unique among the *active* bindings of an Edition, and `series_follow` keyed by
Edition (arbitrage 2, Q3a). Selection and fallback among bindings belong to M1b and M2; the
schema only makes them expressible.
"""

import sqlalchemy as sa
from sqlalchemy.sql.schema import SchemaItem

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    bool_column,
    in_check,
    json_shape_check,
    metadata,
    now_column,
    positive_check,
    prefixed_hash_check,
    ulid_check,
    ulid_column,
)
from shelfhold.infrastructure.schema.catalog import UNIT_TRACKS, lang_check, norm_by_check

DISABLED_REASONS = (
    "not_loaded",
    "content_failures",
    "removed_upstream",
    "manual",
    # M6 L09a (EF-SRC-020, D15): the plugin the binding's source belongs to is uninstalled; the
    # binding stays (the Work, its Units, the exports and the positions are kept) and a reinstall
    # of the same key at the same signer reattaches it
    "orphaned",
)
CONTENT_TRUSTS = ("publisher", "untrusted")  # default untrusted (EF-BIB-051)
BACKFILL_MODES = ("none", "gaps", "all")
UPGRADE_POLICIES = ("keep_first", "prefer_rank")
CHECK_OVERRIDES = ("completed", "paused")  # "terminée", "en pause" (EF-SRC-038)
ALIAS_MIGRATION_STATES = ("previewed", "applied", "cancelled")
SOURCE_MIGRATION_STATES = ("proposed", "validated", "applied", "cancelled")
SOURCE_MIGRATION_BASES = ("same_number", "offset", "manual", "new")
DOWNLOAD_TARGETS = ("unit", "range")
DOWNLOAD_STATES = (
    "requested",
    "downloaded",
    "fetched",
    "cancelled",
    "abandoned",
    "closed_by_purge",
)
DOWNLOAD_UNIT_STATES = ("requested", "covered", "missing")
FOLLOW_REQUEST_STATES = ("approved", "following", "followed", "unfollowed")
FOLLOW_REQUEST_MODES = ("all", "from_number", "new_only")  # = the `FollowMode` of the domain
FAILURE_CLASSES = ("content", "auth", "version", "schema", "infra")
ACCOUNT_KINDS = ("admin", "service")
PLUGIN_ORIGINS = ("builtin", "local")  # `repo` joins in M6
REVISION_NATURES = ("metadata_only", "rewrite")
REVISION_STATES = ("open", "applied", "dismissed")
REVISION_DOORS = ("kavita_api", "rewrite", "forced_rewrite")  # `applied_via`, kept by the code
ATTESTATION_FIELDS = ("content_trust", "publisher", "access_partitioning", "egress")
SOURCE_SECRET_KINDS = ("secret", "cookies")  # a secret field of `settings_schema`, the cookie jar
# What an index can revoke (M6 L07, EF-SRC-017): a plugin by its key, a wheel by its sha256
REVOCATION_KINDS = ("plugin_key", "wheel_sha256")


def _fk(
    column: str, target: str, *, ondelete: str | None = None, nullable: bool = False
) -> sa.Column[str]:
    return sa.Column(
        column, sa.String(26), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable
    )


def sealed_secret_columns() -> list[SchemaItem]:
    """An AEAD envelope (key id, format version, nonce, ciphertext) for secrets, or nothing.

    The cleartext (tokens, passwords, cookie jar) is one sealed JSON document; it never sits in a
    plain column (ENF-SEC-005, EF-SRC-013). The algorithm is read from `secret_key.alg`, so the
    nonce length is not constrained here; a ciphertext is at least one 16-byte AEAD tag.
    """
    return [
        sa.Column("secret_kid", sa.String(), sa.ForeignKey("secret_key.kid"), nullable=True),
        sa.Column("secret_v", sa.Integer(), nullable=True),  # envelope format, part of the AAD
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("secret_ct", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint(
            '("secret_kid" IS NULL AND "secret_v" IS NULL AND "secret_nonce" IS NULL '
            'AND "secret_ct" IS NULL) '
            'OR ("secret_kid" IS NOT NULL AND "secret_v" IS NOT NULL AND "secret_v" >= 1 '
            'AND "secret_nonce" IS NOT NULL AND "secret_ct" IS NOT NULL '
            'AND length("secret_ct") >= 16)',
            name="secret_sealed",
        ),
    ]


# Identity of an upstream series: (plugin, source, remote id). The sub-source is part of the
# key, e.g. `suwayomi:<sourceId>` (EF-SRC-021).
source_item = sa.Table(
    "source_item",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("plugin_id", sa.String(), nullable=False),
    sa.Column("source_key", sa.String(), nullable=False),
    sa.Column("remote_id", sa.String(), nullable=False),
    sa.Column("url", sa.Text(), nullable=True),
    sa.Column("memo", sa.JSON(), nullable=True),
    sa.Column("memo_version", sa.Integer(), nullable=False, server_default="1"),
    sa.Column("last_seen", UtcDateTime(), nullable=True),
    now_column(),
    ulid_check("id"),
    sa.CheckConstraint(
        "\"plugin_id\" <> '' AND \"source_key\" <> '' AND \"remote_id\" <> ''", name="key_not_empty"
    ),
    positive_check("memo_version", minimum=1),
    sa.UniqueConstraint("plugin_id", "source_key", "remote_id"),
)

# Dynamic sub-sources exposed by a meta-source. None is active at installation (ENF-LEG-001).
sub_source = sa.Table(
    "sub_source",
    metadata,
    sa.Column("plugin_id", sa.String(), nullable=False),
    sa.Column("source_key", sa.String(), nullable=False),
    sa.Column("name", sa.String(), nullable=False),
    sa.Column("lang", sa.String(), nullable=False, server_default="und"),  # `mul`: multilingual
    sa.Column("lang_raw", sa.String(), nullable=True),
    sa.Column("content_warning", sa.String(), nullable=True),
    sa.Column("limit_key", sa.String(), nullable=True),
    bool_column("enabled", default=False),
    sa.PrimaryKeyConstraint("plugin_id", "source_key"),
    lang_check("lang", allow_wildcards=True),
)

# Where a series moved upstream: resolved from the M1a schema on, migrated in bulk in M3.
source_alias = sa.Table(
    "source_alias",
    metadata,
    sa.Column("id", sa.Integer(), primary_key=True),
    sa.Column("plugin_id", sa.String(), nullable=False),
    sa.Column("old_source_key", sa.String(), nullable=False),
    sa.Column("old_remote_id", sa.String(), nullable=False),
    sa.Column("new_source_key", sa.String(), nullable=False),
    sa.Column("new_remote_id", sa.String(), nullable=False),
    sa.Column("id_scheme", sa.String(), nullable=True),
    now_column(),
    sa.UniqueConstraint("plugin_id", "old_source_key", "old_remote_id"),
)

source_binding = sa.Table(
    "source_binding",
    metadata,
    ulid_column("id", primary_key=True),
    _fk("work_id", "work.id"),
    _fk("edition_id", "edition.id"),
    _fk("source_item_id", "source_item.id"),
    # rank within the Edition, unique among *active* bindings only (index below)
    sa.Column("priority", sa.Integer(), nullable=False, server_default="1"),
    bool_column("active", default=True),
    sa.Column("disabled_reason", sa.String(), nullable=True),
    # cadence cache computed from observed releases (EF-SRC-038)
    sa.Column("check_cron", sa.String(), nullable=True),
    sa.Column("next_expected", UtcDateTime(), nullable=True),
    sa.Column("last_checked", UtcDateTime(), nullable=True),
    # when the source was last asked to refresh the series (the adaptive cadence, EF-SRC-038)
    sa.Column("last_refreshed_at", UtcDateTime(), nullable=True),
    # declared by the manifest, copied here; the admin may only lower it to `untrusted`
    sa.Column("content_trust", sa.String(), nullable=False, server_default="untrusted"),
    bool_column("linked_from_adoption", default=False),
    now_column(),
    ulid_check("id"),
    positive_check("priority", minimum=1),
    in_check("disabled_reason", DISABLED_REASONS),
    in_check("content_trust", CONTENT_TRUSTS),
    sa.CheckConstraint(
        '("active" AND "disabled_reason" IS NULL) '
        'OR (NOT "active" AND "disabled_reason" IS NOT NULL)',
        name="disabled_reason_matches_active",
    ),
    # a binding ties an Edition to *its own* Work
    sa.ForeignKeyConstraint(["edition_id", "work_id"], ["edition.id", "edition.work_id"]),
    sa.UniqueConstraint("edition_id", "source_item_id"),
    sa.Index("ix_source_binding_work", "work_id"),
    sa.Index("ix_source_binding_source_item", "source_item_id"),
)
sa.Index(
    "uq_source_binding_active_priority",
    source_binding.c.edition_id,
    source_binding.c.priority,
    unique=True,
    sqlite_where=sa.text('"active"'),
)
# An upstream series is followed by one Edition at a time: a second active binding on the same
# `source_item` is a duplicate (L42 review).
sa.Index(
    "uq_source_binding_active_source_item",
    source_binding.c.source_item_id,
    unique=True,
    sqlite_where=sa.text('"active"'),
)

# What the follow cycle has told the admin and what still waits for them (L42 review): one row per
# condition, deleted when the condition ceases, so a new process never tells it twice. `key_hash`
# is a digest of the upstream key (no title, no path); `subject` is the identifier the exit command
# takes (an ULID), empty when there is none.
follow_notice = sa.Table(
    "follow_notice",
    metadata,
    sa.Column("kind", sa.String(), nullable=False),
    sa.Column("key_hash", sa.String(), nullable=False),
    sa.Column("subject", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("told_at", UtcDateTime(), nullable=False),
    sa.PrimaryKeyConstraint("kind", "key_hash"),
    in_check("kind", ("tombstoned", "awaiting_shelf", "pending_pairing", "pairing_declined")),
)

# One follow per Edition, whatever its bindings (arbitrage 2). The watermark is the pair
# (segment, number) of EF-BIB-050: nothing at or below it is requested by the first cycle.
series_follow = sa.Table(
    "series_follow",
    metadata,
    _fk("edition_id", "edition.id", ondelete="CASCADE"),
    _fk("work_id", "work.id"),
    sa.Column("follow_from_segment", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("follow_from_number", sa.String(), nullable=True),  # NULL: no watermark yet
    sa.Column("follow_from_norm_by", sa.String(), nullable=True),
    sa.Column("backfill_mode", sa.String(), nullable=False, server_default="none"),
    sa.Column("cap", sa.Integer(), nullable=True),  # per cycle, in Units
    sa.Column("upgrade_policy", sa.String(), nullable=False, server_default="keep_first"),
    sa.Column("check_override", sa.String(), nullable=True),
    # NULL: inherit the shelf's filters (EF-SRC-039); otherwise a JSON object
    sa.Column("filters", sa.JSON(none_as_null=True), nullable=True),
    now_column(),
    sa.Column("updated_at", UtcDateTime(), nullable=True),
    sa.PrimaryKeyConstraint("edition_id"),
    sa.ForeignKeyConstraint(["edition_id", "work_id"], ["edition.id", "edition.work_id"]),
    in_check("backfill_mode", BACKFILL_MODES),
    in_check("upgrade_policy", UPGRADE_POLICIES),
    in_check("check_override", CHECK_OVERRIDES),
    positive_check("cap", minimum=1),
    norm_by_check("follow_from_norm_by"),
    sa.CheckConstraint(
        '"filters" IS NULL OR CASE WHEN json_valid("filters") '
        "THEN json_type(\"filters\") = 'object' ELSE 0 END",
        name="filters_json_object",
    ),
)

# A gap the admin accepted: it is not a hole to backfill (EF-BIB-056).
gap_ack = sa.Table(
    "gap_ack",
    metadata,
    _fk("edition_id", "edition.id", ondelete="CASCADE"),
    sa.Column("track", sa.String(), nullable=False),
    sa.Column("number_segment", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("number_norm", sa.String(), nullable=False),
    sa.Column("acked_by", sa.String(26), sa.ForeignKey("user.id", ondelete="SET NULL")),
    sa.Column("acked_at", UtcDateTime(), nullable=False),
    sa.PrimaryKeyConstraint("edition_id", "track", "number_segment", "number_norm"),
    in_check("track", UNIT_TRACKS),
    sa.CheckConstraint("\"number_norm\" <> ''", name="number_norm_not_empty"),
)

# The persistent ticket of the connected service's `server_queue` capability. Only chapters
# Shelfhold asked for may be retried (EF-SRC-026). `closed_by_purge`: the Unit was erased.
download_request = sa.Table(
    "download_request",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("target_kind", sa.String(), nullable=False),
    _fk("variant_id", "unit_variant.id", ondelete="SET NULL", nullable=True),
    # the Unit a `unit` ticket asks for (I52): what makes "one open request per Unit" a property
    # of the database, whatever Variant, binding or worker asks
    # (no foreign key: a plain ADD COLUMN, and the purge closes the ticket by its state)
    sa.Column("unit_id", sa.String(26), nullable=True),
    sa.Column("range_ref", sa.JSON(), nullable=True),  # edition, first and last Unit, volume label
    _fk("source_binding_id", "source_binding.id"),
    sa.Column("ticket", sa.String(), nullable=True),  # opaque; set once the service answered
    sa.Column("state", sa.String(), nullable=False, server_default="requested"),
    now_column("requested_at"),
    sa.Column("updated_at", UtcDateTime(), nullable=True),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    # D19 / EF-USR-016: who asked and who may see it. Null: an operation, for the admin only.
    sa.Column("visibility", sa.String(), nullable=True),  # private | household | ops
    sa.Column("requester_user_id", sa.String(26), nullable=True),  # `user.id`, set from M3
    ulid_check("id"),
    in_check("target_kind", DOWNLOAD_TARGETS),
    in_check("state", DOWNLOAD_STATES),
    positive_check("attempts"),
    sa.CheckConstraint(
        '("target_kind" = \'unit\' AND ("variant_id" IS NOT NULL OR "state" = \'closed_by_purge\') '
        'AND "range_ref" IS NULL) '
        'OR ("target_kind" = \'range\' AND "variant_id" IS NULL AND "range_ref" IS NOT NULL)',
        name="target_matches_kind",
    ),
    sa.Index("ix_download_request_state", "state"),
    sa.Index("ix_download_request_variant", "variant_id"),
)
sa.Index(
    "uq_download_request_ticket",
    download_request.c.source_binding_id,
    download_request.c.ticket,
    unique=True,
    sqlite_where=sa.text('"ticket" IS NOT NULL'),
)

# One open ticket per Variant, whatever the writers (two cycles, a retry): the database says no.
sa.Index(
    "uq_download_request_open_variant",
    download_request.c.variant_id,
    unique=True,
    sqlite_where=sa.text("\"state\" IN ('requested', 'downloaded')"),
)

# One open ticket per Unit (I52, EF-SRC-037): two bindings that both offer a Unit are never both
# asked, even by two workers at once. A range ticket carries no `unit_id` and is out of it.
sa.Index(
    "uq_download_request_open_unit",
    download_request.c.unit_id,
    unique=True,
    sqlite_where=sa.text("\"state\" IN ('requested', 'downloaded')"),
)

# A request to follow a series of the connected service from Shelfhold (EF-SRC-029), approved. One
# row per upstream series: the unique key is what makes the entry in the service's library unique
# whatever the writers (a replay, a double click, two workers). `following` is the claim of the one
# that is calling the service (`claimed_at` lets another take over after a crash; the call itself
# is idempotent). Same visibility rules as `download_request` (D19, EF-USR-016).
follow_request = sa.Table(
    "follow_request",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("source_key", sa.String(), nullable=False),  # `suwayomi:<sourceId>`
    sa.Column("remote_id", sa.String(), nullable=False),  # the upstream series (relative url or id)
    sa.Column("state", sa.String(), nullable=False, server_default="approved"),
    sa.Column("visibility", sa.String(), nullable=False, server_default="ops"),
    sa.Column("requester_user_id", sa.String(26), nullable=True),  # `user.id`; null: the host CLI
    sa.Column("claimed_at", UtcDateTime(), nullable=True),
    # what the add sheet chose (M3 L42): NULL mode, the series was added without a choice
    sa.Column("mode", sa.String(), nullable=True),
    sa.Column("from_number", sa.String(), nullable=True),
    _fk("shelf_id", "shelf.id", ondelete="SET NULL", nullable=True),
    now_column(),
    sa.Column("updated_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    in_check("state", FOLLOW_REQUEST_STATES),
    in_check("mode", FOLLOW_REQUEST_MODES),
    in_check("visibility", ("private", "household", "ops")),
    sa.UniqueConstraint("source_key", "remote_id"),
)

# The Units a ticket covers; the per-cycle cap of EF-EXP-053 counts these.
download_request_unit = sa.Table(
    "download_request_unit",
    metadata,
    _fk("download_request_id", "download_request.id", ondelete="CASCADE"),
    _fk("unit_id", "unit.id", ondelete="CASCADE"),
    sa.Column("state", sa.String(), nullable=False, server_default="requested"),
    sa.PrimaryKeyConstraint("download_request_id", "unit_id"),
    in_check("state", DOWNLOAD_UNIT_STATES),
    sa.Index("ix_download_request_unit_unit", "unit_id"),
)

# Counted per upstream source; only `content` errors make a Unit irreplaceable (EF-BIB-042).
source_failure = sa.Table(
    "source_failure",
    metadata,
    _fk("source_binding_id", "source_binding.id", ondelete="CASCADE"),
    sa.Column("limit_key", sa.String(), nullable=False),
    sa.Column("failure_class", sa.String(), nullable=False),
    sa.Column("consecutive_content_errors", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("last_error_code", sa.String(), nullable=True),
    sa.Column("last_error_at", UtcDateTime(), nullable=True),
    sa.PrimaryKeyConstraint("source_binding_id", "limit_key", "failure_class"),
    in_check("failure_class", FAILURE_CLASSES),
    positive_check("consecutive_content_errors"),
)

source_config = sa.Table(
    "source_config",
    metadata,
    sa.Column("plugin_id", sa.String(), primary_key=True),
    sa.Column("settings", sa.JSON(), nullable=False, server_default="{}"),  # nothing secret
    *sealed_secret_columns(),  # secret settings and cookie jar
    sa.Column("user_agent", sa.String(), nullable=True),
    sa.Column("rate_limit_override", sa.JSON(), nullable=True),
    sa.Column("terms_url", sa.Text(), nullable=True),
    sa.Column("health", sa.JSON(), nullable=True),  # last good download, failure rate, freshness
    sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("rights_basis", sa.String(), nullable=True),
    bool_column("family_safe", default=False),  # marked "family" by the admin (EF-USR-014)
    # What the connected service said it is (EF-SRC-036): a change means another install.
    sa.Column("instance_fingerprint", sa.String(), nullable=True),
    sa.Column("instance_fingerprint_seen_at", UtcDateTime(), nullable=True),
    now_column(),
    sa.Column("updated_at", UtcDateTime(), nullable=True),
    positive_check("consecutive_failures"),
)

# Per-user credentials of a connected service (EF-SRC-023). `kind`: the admin's own account or a
# dedicated service account (M1b); `auth_mode`: what the service answered (`basic_auth`, ...).
source_account = sa.Table(
    "source_account",
    metadata,
    ulid_column("id", primary_key=True),
    _fk("user_id", "user.id", ondelete="CASCADE"),
    sa.Column("plugin_id", sa.String(), nullable=False),
    sa.Column("kind", sa.String(), nullable=False, server_default="admin"),
    sa.Column("auth_mode", sa.String(), nullable=True),
    sa.Column("auth_kind", sa.String(), nullable=True),
    *sealed_secret_columns(),
    sa.Column("session_expires_at", UtcDateTime(), nullable=True),
    now_column(),
    ulid_check("id"),
    in_check("kind", ACCOUNT_KINDS),
    sa.UniqueConstraint("user_id", "plugin_id", "kind"),
)

# Durable record of upstream settings taken at adoption, for `detach --restore-upstream`.
# No automatic retention: purged only with the detachment (I31).
upstream_snapshot = sa.Table(
    "upstream_snapshot",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("connected_service_id", sa.String(), nullable=False),
    sa.Column("series_ref", sa.String(), nullable=False),
    sa.Column("capability", sa.String(), nullable=False),
    sa.Column("payload", sa.JSON(), nullable=False),
    sa.Column("schema_version", sa.Integer(), nullable=False),
    sa.Column("taken_at", UtcDateTime(), nullable=False),
    ulid_check("id"),
    positive_check("schema_version", minimum=1),
    sa.Index("ix_upstream_snapshot_series", "connected_service_id", "series_ref", "capability"),
)

# Third-party plugins are installed disabled (EF-SRC-019). Since M6 L08 (EF-SRC-017 passe 8,
# GAP-8-13) the row also keeps the pin of an install from a signed index: the repository's URL
# and the public key of its signer. A wheel the admin gave by hand has neither (the pin is the
# admin's own act); a plugin installed from an index has both — and another signer publishing the
# same key is a new install, never an update. Since M6 L09a (EF-SRC-020, D15) the row also keeps
# the lifecycle: `pinned` freezes the installed version (no update, no rollback), and the previous
# kept version (`previous_version`, `previous_sha256`) is the one — and the only one — the
# reserved rollback returns to; the pair comes and goes together (`previous_paired`).
# same key is a new install, never an update.
plugin_install = sa.Table(
    "plugin_install",
    metadata,
    sa.Column("plugin_id", sa.String(), primary_key=True),
    sa.Column("version", sa.String(), nullable=False),
    sa.Column("api", sa.String(), nullable=False),
    sa.Column("origin", sa.String(), nullable=False),
    sa.Column("sha256", sa.String(), nullable=False),
    bool_column("enabled", default=False),
    sa.Column("state_schema", sa.Integer(), nullable=True),
    sa.Column("index_url", sa.Text(), nullable=True),
    sa.Column("signer_pubkey", sa.Text(), nullable=True),
    bool_column("pinned", default=False),
    sa.Column("previous_version", sa.Text(), nullable=True),
    sa.Column("previous_sha256", sa.Text(), nullable=True),
    # M6 L09b (EF-SRC-030): the minimum host version the installed version's index entry
    # declared — what a host upgrade is judged against at the next start-up
    sa.Column("min_host", sa.Text(), nullable=True),
    now_column("installed_at"),
    prefixed_hash_check("sha256"),
    prefixed_hash_check("previous_sha256"),
    in_check("origin", PLUGIN_ORIGINS),
    sa.CheckConstraint(
        '("index_url" IS NULL AND "signer_pubkey" IS NULL)'
        ' OR ("index_url" IS NOT NULL AND "signer_pubkey" IS NOT NULL)',
        name="pin_paired",
    ),
    sa.CheckConstraint(
        '("previous_version" IS NULL AND "previous_sha256" IS NULL)'
        ' OR ("previous_version" IS NOT NULL AND "previous_sha256" IS NOT NULL)',
        name="previous_paired",
    ),
)

plugin_state = sa.Table(
    "plugin_state",
    metadata,
    sa.Column("plugin_id", sa.String(), nullable=False),
    sa.Column("key", sa.String(), nullable=False),
    sa.Column("payload", sa.JSON(), nullable=False),
    sa.Column("payload_schema", sa.Integer(), nullable=False, server_default="1"),
    # M6 L09b (EF-SRC-030): the version of the plugin that wrote the document — the rollback
    # is refused toward a version that cannot read it
    sa.Column("state_written_by_version", sa.Text(), nullable=True),
    sa.Column("updated_at", UtcDateTime(), nullable=True),
    sa.PrimaryKeyConstraint("plugin_id", "key"),
    positive_check("payload_schema", minimum=1),
)

# Revisions seen before M2: never applied in bulk (EF-BIB-043).
revision_pending = sa.Table(
    "revision_pending",
    metadata,
    ulid_column("id", primary_key=True),
    _fk("variant_id", "unit_variant.id", ondelete="CASCADE"),
    sa.Column("revision", sa.Integer(), nullable=False),
    sa.Column("nature", sa.String(), nullable=False),
    sa.Column("koreader_impact", sa.String(), nullable=True),  # estimate, free-form until M2
    sa.Column("state", sa.String(), nullable=False, server_default="open"),
    now_column(),
    sa.Column("resolved_at", UtcDateTime(), nullable=True),
    # M2 L17: the metadata field a `metadata_only` revision carries and its new value (what the
    # Kavita API is asked to set), and the door a resolved row went through.
    sa.Column("field", sa.String(), nullable=True),
    sa.Column("value", sa.String(), nullable=True),
    sa.Column("applied_via", sa.String(), nullable=True),
    ulid_check("id"),
    in_check("nature", REVISION_NATURES),
    in_check("state", REVISION_STATES),
    positive_check("revision", minimum=1),
    sa.UniqueConstraint("variant_id", "revision"),
)

# What the admin attested about a plugin whose manifest cannot say it (EF-SRC-024, ENF-LEG-007):
# one value per plugin and per field, kept when the admin account goes.
plugin_attestation = sa.Table(
    "plugin_attestation",
    metadata,
    sa.Column("plugin_key", sa.String(), nullable=False),
    sa.Column("field", sa.String(), nullable=False),
    sa.Column("value", sa.String(), nullable=False),
    sa.Column("attested_by", sa.String(26), sa.ForeignKey("user.id", ondelete="SET NULL")),
    sa.Column("attested_at", UtcDateTime(), nullable=False),
    # The installed version it was given for (EF-EXP-065): another version is code nobody vouched
    # for, so the attestation lapses; '' (rows from before 0027) matches no version.
    sa.Column("plugin_version", sa.String(), nullable=False, server_default=sa.text("''")),
    # ... and the digest of the installed files: a new wheel under the same version lapses it too
    sa.Column("plugin_sha256", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.PrimaryKeyConstraint("plugin_key", "field"),
    in_check("field", ATTESTATION_FIELDS),
    sa.CheckConstraint("\"plugin_key\" <> ''", name="plugin_key_not_empty"),
)

# A repository of signed plugin indexes the admin pinned (M6 L07, EF-SRC-017, D14): by its URL
# and by the public key of its signer, never on the repository's own word. `key_id` is the
# fingerprint shown everywhere; `last_version` is the anti-rollback floor (a repository added
# back starts from zero); `content_sha256` is the document of that number, so two indexes for
# one version are refused. No repository is preconfigured.
plugin_repo = sa.Table(
    "plugin_repo",
    metadata,
    sa.Column("url", sa.Text(), primary_key=True),
    sa.Column("pubkey", sa.Text(), nullable=False),
    sa.Column("key_id", sa.String(16), nullable=False),
    sa.Column("last_version", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("expires", UtcDateTime(), nullable=True),
    sa.Column("content_sha256", sa.String(), nullable=True),
    now_column("added_at"),
    sa.CheckConstraint("\"pubkey\" <> ''", name="pubkey_not_empty"),
    sa.CheckConstraint("\"key_id\" <> ''", name="key_id_not_empty"),
    positive_check("last_version", minimum=0),
    prefixed_hash_check("content_sha256"),
)

# A revocation seen in a signed index stays seen, even if a later index omits it (D14): a plugin
# by its key, a wheel by its sha256. One row per revocation.
plugin_revocation = sa.Table(
    "plugin_revocation",
    metadata,
    sa.Column("kind", sa.String(), primary_key=True),
    sa.Column("value", sa.Text(), primary_key=True),
    now_column("seen_at"),
    in_check("kind", REVOCATION_KINDS),
    sa.CheckConstraint("\"value\" <> ''", name="value_not_empty"),
)

# One sealed value per source and field (EF-SRC-013, ENF-SEC-005): a secret field of the source's
# `settings_schema`, or its cookie jar (`kind = cookies`, field `jar`). The id is
# `<plugin>/<kind>/<field>/<generation>` and it is the row coordinate of the envelope's associated
# data, so a sealed value copied to another source or field, or an older one put back, does not
# open. `fingerprint` is a short keyed
# digest the API shows instead of the value; nothing reads a secret back.
source_secret = sa.Table(
    "source_secret",
    metadata,
    sa.Column("id", sa.String(), primary_key=True),
    sa.Column("plugin_id", sa.String(), nullable=False),
    sa.Column("kind", sa.String(), nullable=False),
    sa.Column("field", sa.String(), nullable=False),
    sa.Column("fingerprint", sa.String(), nullable=False),
    # Bumped on every write and part of the id, so an older envelope put back does not open
    sa.Column("generation", sa.Integer(), nullable=False),
    *sealed_secret_columns(),
    now_column("updated_at"),
    in_check("kind", SOURCE_SECRET_KINDS),
    sa.CheckConstraint(
        '"id" = "plugin_id" || \'/\' || "kind" || \'/\' || "field" || \'/\' || "generation"',
        name="id_coordinates",
    ),
    positive_check("generation", minimum=1),
    sa.CheckConstraint('"secret_ct" IS NOT NULL', name="sealed"),
    sa.CheckConstraint("\"plugin_id\" <> '' AND \"field\" <> ''", name="named"),
    sa.UniqueConstraint("plugin_id", "kind", "field"),
)

# The key of a metadata provider that is the instance's own (Hardcover token, Google Books key;
# M4 L21a, EF-BIB-013): one row per provider, the key in the sealed columns and nowhere else. The
# provider is the row id, so the envelope is bound to it (an envelope copied to another row does
# not open). Which providers have a key is the code's business (`provider_keys.PROVIDERS`).
provider_secret = sa.Table(
    "provider_secret",
    metadata,
    sa.Column("provider", sa.String(), primary_key=True),
    *sealed_secret_columns(),
    now_column("updated_at"),
    sa.CheckConstraint('"provider" <> \'\' AND length("provider") <= 40', name="named"),
)

# A hole seen in an Edition's numbering; the row is what makes the notification one per gap
# (EF-BIB-056). Same key as `gap_ack`, so the two join without translation.
series_gap = sa.Table(
    "series_gap",
    metadata,
    _fk("edition_id", "edition.id", ondelete="CASCADE"),
    sa.Column("track", sa.String(), nullable=False),
    sa.Column("number_segment", sa.String(), nullable=False, server_default=sa.text("''")),
    sa.Column("number_norm", sa.String(), nullable=False),
    sa.Column("first_seen_at", UtcDateTime(), nullable=False),
    sa.Column("notified_at", UtcDateTime(), nullable=True),
    sa.PrimaryKeyConstraint("edition_id", "track", "number_segment", "number_norm"),
    in_check("track", UNIT_TRACKS),
    sa.CheckConstraint("\"number_norm\" <> ''", name="number_norm_not_empty"),
)

# The release times observed on a binding: the cadence cache (`check_cron`, `next_expected`) is
# computed from the latest of them (EF-SRC-038).
binding_release_sample = sa.Table(
    "binding_release_sample",
    metadata,
    _fk("source_binding_id", "source_binding.id", ondelete="CASCADE"),
    sa.Column("observed_at", UtcDateTime(), nullable=False),
    sa.PrimaryKeyConstraint("source_binding_id", "observed_at"),
)

# A remote result not yet made a Work (EF-USR-010): what a request on `source_ref` points at.
pending_source_item = sa.Table(
    "pending_source_item",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("source_key", sa.String(), nullable=False),
    sa.Column("series_ref", sa.String(), nullable=False),
    sa.Column("title", sa.String(), nullable=False),
    sa.Column("fetched_at", UtcDateTime(), nullable=False),
    sa.Column("memo", sa.JSON(), nullable=True),  # what the search knew (M3 L35, migration 0057)
    ulid_check("id"),
    sa.CheckConstraint(
        "\"source_key\" <> '' AND \"series_ref\" <> '' AND \"title\" <> ''", name="named"
    ),
    sa.UniqueConstraint("source_key", "series_ref"),
)

# A Mihon backup (`.tachibk`) imported by an admin for one profile (M4 L26, EF-INT-008, migration
# 0095): read as a hostile file, previewed (`state = 'previewed'`, `staged` holds what would be
# written, which is all the library sees of it), then applied once. `content_hash` identifies the
# file, never its content; no name of a source is ever kept. The staged text is dropped on apply.
TACHIBK_IMPORT_STATES = ("previewed", "applied")
TRACKER_NAMES = ("myanimelist", "anilist", "kitsu", "mangaupdates", "shikimori", "bangumi")
tachibk_import = sa.Table(
    "tachibk_import",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("created_by", sa.String(26), sa.ForeignKey("user.id", ondelete="SET NULL")),
    sa.Column("schema_version", sa.String(), nullable=False),
    sa.Column("content_hash", sa.String(64), nullable=False),
    sa.Column("state", sa.String(), nullable=False, server_default="previewed"),
    sa.Column("staged", sa.Text(), nullable=True),
    sa.Column("summary", sa.JSON(), nullable=False, server_default="{}"),
    now_column(),
    sa.Column("applied_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    in_check("state", TACHIBK_IMPORT_STATES),
    json_shape_check("summary", "object"),
    sa.CheckConstraint('length("content_hash") = 64', name="content_hash_length"),
    sa.Index("ix_tachibk_import_user", "user_id"),
)

# One preview of the switch of an Edition to its preferred team, then its application (M4 L35,
# EF-BIB-005): `preview` is what the admin read (the Units that would switch, the blocked ones, the
# pending ones), `token` its digest, compared with the one of the application so that a preview
# gone stale is never applied; `report` is what was done. No path and no name is kept.
TEAM_SWITCH_STATES = ("previewed", "queued", "applying", "done", "stale", "failed")
team_switch = sa.Table(
    "team_switch",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "edition_id", sa.String(26), sa.ForeignKey("edition.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column(
        "team_id", sa.String(26), sa.ForeignKey("team.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("created_by", sa.String(26), sa.ForeignKey("user.id", ondelete="SET NULL")),
    sa.Column("state", sa.String(), nullable=False, server_default="previewed"),
    sa.Column("token", sa.String(64), nullable=False),
    sa.Column("preview", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("report", sa.JSON(), nullable=True),
    now_column(),
    sa.Column("applied_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    in_check("state", TEAM_SWITCH_STATES),
    json_shape_check("preview", "object"),
    sa.CheckConstraint('length("token") = 64', name="token_length"),
    sa.Index("ix_team_switch_edition", "edition_id"),
)

# What a profile read in the series of a backup, waiting for the series to be resolved (EF-INT-008):
# one row per chapter the file proves read or started, for the profile the admin chose and nobody
# else. A marker is not a reading position: it says what the backup proved, and nothing more.
read_marker = sa.Table(
    "read_marker",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column(
        "pending_item_id",
        sa.String(26),
        sa.ForeignKey("pending_source_item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("import_id", sa.String(26), sa.ForeignKey("tachibk_import.id", ondelete="SET NULL")),
    sa.Column("chapter_ref", sa.String(), nullable=False),
    sa.Column("number", sa.String(), nullable=True),
    bool_column("read", default=False),
    sa.Column("last_page", sa.Integer(), nullable=True),
    now_column(),
    ulid_check("id"),
    sa.CheckConstraint('"read" IN (0, 1)', name="read_bool"),
    sa.CheckConstraint('length("chapter_ref") BETWEEN 1 AND 2048', name="chapter_ref_length"),
    sa.CheckConstraint('"last_page" IS NULL OR "last_page" >= 0', name="last_page_min"),
    sa.UniqueConstraint("user_id", "pending_item_id", "chapter_ref"),
    sa.Index("ix_read_marker_item", "pending_item_id"),
)

# The tracker entry a series was linked to in the backup: which tracker, which entry, how far the
# reader was. An identifier, never an address, a title or a token (EF-INT-008).
tracker_hint = sa.Table(
    "tracker_hint",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "user_id", sa.String(26), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column(
        "pending_item_id",
        sa.String(26),
        sa.ForeignKey("pending_source_item.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("import_id", sa.String(26), sa.ForeignKey("tachibk_import.id", ondelete="SET NULL")),
    sa.Column("tracker", sa.String(), nullable=False),
    sa.Column("remote_id", sa.String(), nullable=False),
    sa.Column("last_chapter", sa.Float(), nullable=True),
    now_column(),
    ulid_check("id"),
    in_check("tracker", TRACKER_NAMES),
    sa.CheckConstraint('length("remote_id") BETWEEN 1 AND 64', name="remote_id_length"),
    sa.UniqueConstraint("user_id", "pending_item_id", "tracker"),
    sa.Index("ix_tracker_hint_item", "pending_item_id"),
)

# A bulk change of the identity scheme of a source, previewed before it is applied (EF-SRC-022).
source_alias_migration = sa.Table(
    "source_alias_migration",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("source_key", sa.String(), nullable=False),
    sa.Column("from_scheme", sa.String(), nullable=False),
    sa.Column("to_scheme", sa.String(), nullable=False),
    sa.Column("state", sa.String(), nullable=False, server_default="previewed"),
    sa.Column("preview", sa.JSON(), nullable=False, server_default="{}"),
    now_column(),
    ulid_check("id"),
    in_check("state", ALIAS_MIGRATION_STATES),
    json_shape_check("preview", "object"),
    sa.CheckConstraint('"from_scheme" <> "to_scheme"', name="schemes_differ"),
    sa.CheckConstraint("\"source_key\" <> ''", name="source_key_not_empty"),
    sa.Index("ix_source_alias_migration_source", "source_key"),
)

# The migration of one series to another source (M4 L27, EF-BIB-014): a numbering correspondence
# proposed from what the new source lists, validated by the admin as it was shown, then applied in
# one transaction. `digest` is the fingerprint of the table, `held_digest` that of the Units the
# Edition held when it was made: a table is applied only against the Edition it was made for.
source_migration = sa.Table(
    "source_migration",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column(
        "edition_id",
        sa.String(26),
        sa.ForeignKey("edition.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "work_id", sa.String(26), sa.ForeignKey("work.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("plugin_id", sa.String(), nullable=False),
    sa.Column("source_key", sa.String(), nullable=False),
    sa.Column("remote_id", sa.String(), nullable=False),
    sa.Column("memo", sa.JSON(), nullable=False, server_default="{}"),
    sa.Column("memo_version", sa.Integer(), nullable=True),
    sa.Column(
        "replaces_binding_id",
        sa.String(26),
        sa.ForeignKey("source_binding.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column(
        "binding_id",
        sa.String(26),
        sa.ForeignKey("source_binding.id", ondelete="SET NULL"),
        nullable=True,
    ),
    sa.Column("state", sa.String(), nullable=False, server_default="proposed"),
    sa.Column("number_offset", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("digest", sa.String(64), nullable=False),
    sa.Column("held_digest", sa.String(64), nullable=False),
    now_column(),
    sa.Column("validated_at", UtcDateTime(), nullable=True),
    sa.Column("applied_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    ulid_check("edition_id"),
    in_check("state", SOURCE_MIGRATION_STATES),
    json_shape_check("memo", "object"),
    sa.CheckConstraint(
        "\"plugin_id\" <> '' AND \"source_key\" <> '' AND \"remote_id\" <> ''", name="named"
    ),
    sa.CheckConstraint('"number_offset" BETWEEN -100000 AND 100000', name="offset_range"),
    sa.CheckConstraint('length("digest") = 64 AND length("held_digest") = 64', name="digests"),
    sa.CheckConstraint(
        '("state" = \'applied\') = ("applied_at" IS NOT NULL)', name="applied_is_dated"
    ),
    sa.Index("ix_source_migration_edition", "edition_id"),
)
# One open table per (Edition, fingerprint): proposing the same listing twice is one table.
sa.Index(
    "uq_source_migration_open",
    source_migration.c.edition_id,
    source_migration.c.digest,
    unique=True,
    sqlite_where=sa.text("\"state\" IN ('proposed', 'validated')"),
)

# One line of that table: a number of the new source (`place`, as the follow places it), the
# number it takes in the library (`target`, the library's own numbering, never the source's) and
# the Unit it lands on, or none when the library lacks it. Two lines never land on one place.
source_migration_row = sa.Table(
    "source_migration_row",
    metadata,
    sa.Column(
        "migration_id",
        sa.String(26),
        sa.ForeignKey("source_migration.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column("place", sa.String(), nullable=False),
    sa.Column("number", sa.String(), nullable=True),
    sa.Column("source_order", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("target", sa.String(), nullable=True),
    sa.Column("target_order", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("target_place", sa.String(), nullable=False),
    sa.Column(
        "unit_id", sa.String(26), sa.ForeignKey("unit.id", ondelete="SET NULL"), nullable=True
    ),
    sa.Column("basis", sa.String(), nullable=False),
    sa.PrimaryKeyConstraint("migration_id", "place"),
    ulid_check("migration_id"),
    in_check("basis", SOURCE_MIGRATION_BASES),
    positive_check("source_order"),
    positive_check("target_order"),
    sa.UniqueConstraint("migration_id", "target_place"),
    sa.Index("ix_source_migration_row_unit", "unit_id"),
)

# A chapter the administrator does not want back (M4 L29, EF-INT-019): by what the source calls it,
# `(source_key, remote_unit_id)`, so it holds whether or not a Unit, a binding or even the series
# still exists here. Only an explicit removal takes a row away: the account that made it being
# deleted leaves `created_by` empty, never the block. `proposed_count` and `last_proposed_at` are
# how often the source offered it again and was refused.
BLOCK_REASONS = ("bad_quality", "wrong_content", "duplicate", "other")
activity_block = sa.Table(
    "activity_block",
    metadata,
    ulid_column("id", primary_key=True),
    sa.Column("source_key", sa.String(), nullable=False),
    sa.Column("remote_unit_id", sa.String(), nullable=False),
    sa.Column("reason", sa.String(), nullable=False),
    _fk("created_by", "user.id", ondelete="SET NULL", nullable=True),
    now_column(),
    sa.Column("proposed_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
    sa.Column("last_proposed_at", UtcDateTime(), nullable=True),
    ulid_check("id"),
    in_check("reason", BLOCK_REASONS),
    positive_check("proposed_count"),
    sa.CheckConstraint(
        "\"source_key\" <> '' AND \"remote_unit_id\" <> ''", name="source_and_unit_named"
    ),
    sa.UniqueConstraint("source_key", "remote_unit_id"),
)
