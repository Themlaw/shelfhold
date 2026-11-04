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
    metadata,
    now_column,
    positive_check,
    prefixed_hash_check,
    ulid_check,
    ulid_column,
)
from shelfhold.infrastructure.schema.catalog import UNIT_TRACKS, lang_check, norm_by_check

DISABLED_REASONS = ("not_loaded", "content_failures", "removed_upstream", "manual")
CONTENT_TRUSTS = ("publisher", "untrusted")  # default untrusted (EF-BIB-051)
BACKFILL_MODES = ("none", "gaps", "all")
UPGRADE_POLICIES = ("keep_first", "prefer_rank")
CHECK_OVERRIDES = ("completed", "paused")  # "terminée", "en pause" (EF-SRC-038)
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
FAILURE_CLASSES = ("content", "auth", "version", "schema", "infra")
ACCOUNT_KINDS = ("admin", "service")
PLUGIN_ORIGINS = ("builtin", "local")  # `repo` joins in M6
REVISION_NATURES = ("metadata_only", "rewrite")
REVISION_STATES = ("open", "applied", "dismissed")


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
    sa.Column("range_ref", sa.JSON(), nullable=True),  # edition, first and last Unit, volume label
    _fk("source_binding_id", "source_binding.id"),
    sa.Column("ticket", sa.String(), nullable=True),  # opaque; set once the service answered
    sa.Column("state", sa.String(), nullable=False, server_default="requested"),
    now_column("requested_at"),
    sa.Column("updated_at", UtcDateTime(), nullable=True),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
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

# Third-party plugins are installed disabled (EF-SRC-019).
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
    now_column("installed_at"),
    prefixed_hash_check("sha256"),
    in_check("origin", PLUGIN_ORIGINS),
)

plugin_state = sa.Table(
    "plugin_state",
    metadata,
    sa.Column("plugin_id", sa.String(), nullable=False),
    sa.Column("key", sa.String(), nullable=False),
    sa.Column("payload", sa.JSON(), nullable=False),
    sa.Column("payload_schema", sa.Integer(), nullable=False, server_default="1"),
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
    ulid_check("id"),
    in_check("nature", REVISION_NATURES),
    in_check("state", REVISION_STATES),
    positive_check("revision", minimum=1),
    sa.UniqueConstraint("variant_id", "revision"),
)
