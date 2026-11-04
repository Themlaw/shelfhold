"""M1a schema, storage / export / reading hooks (PLAN-M1a L04).

Last free migration of M1a. It also lands what the earlier revisions announced: `edition.shelf_id`
(nullable, foreign key to `shelf`), the foreign key of `export_plan.shelf_destination_id`, the
`physical_root.mode` column, and the same-parent key of `unit_variant`. The existing tables are
rebuilt by SQLite batch mode, which is safe because migrations run with `foreign_keys=OFF`
followed by `foreign_key_check` (L02). The revision is a frozen snapshot: it does not import the
live metadata, and `test_metadata_matches_migrations` and `test_migrated_ddl_equals_create_all`
keep the two honest.
"""

# ruff: noqa: E501  (frozen, generated constraint expressions)

from contextlib import AbstractContextManager
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from alembic.operations.base import BatchOperations

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

TABLES = (
    "replica",
    "export_record",
    "document_alias",
    "export_item",
    "content",
    "unit_coverage",
    "field_owner",
    "edition_export_series",
    "export_series",
    "shelf_destination_remote_library",
    "kavita_access_audit",
    "shelf_destination",
    "remote_library",
    "kavita_instance",
    "trash_item",
    "reading_position",
    "export_target",
    "shelf",
)

# Triggers of the coverage carriers: a coverage never outlives the thing that carried it. The
# statements are copies of the ones in infrastructure/schema/export.py.
TRIGGERS = {
    "export_item_coverage_cleanup": (
        "CREATE TRIGGER export_item_coverage_cleanup "
        "AFTER DELETE ON export_item "
        "BEGIN DELETE FROM unit_coverage "
        "WHERE carrier_type = 'export_item' AND carrier_id = OLD.id; END"
    ),
    "content_coverage_cleanup": (
        "CREATE TRIGGER content_coverage_cleanup "
        "AFTER DELETE ON content "
        "BEGIN DELETE FROM unit_coverage "
        "WHERE carrier_type = 'content' AND carrier_id = OLD.id; END"
    ),
    "volume_coverage_cleanup": (
        "CREATE TRIGGER volume_coverage_cleanup "
        "AFTER DELETE ON volume "
        "BEGIN DELETE FROM unit_coverage "
        "WHERE carrier_type = 'volume' AND carrier_id = OLD.id; END"
    ),
    "unit_coverage_cleanup": (
        "CREATE TRIGGER unit_coverage_cleanup "
        "AFTER DELETE ON unit "
        "BEGIN DELETE FROM unit_coverage "
        "WHERE carrier_type = 'unit' AND carrier_id = OLD.id; END"
    ),
}

READING_PROGRESS = """
SELECT user_id, unit_id,
  CASE WHEN done THEN 'read' WHEN started THEN 'in_progress' ELSE 'unread' END AS state,
  position_id AS last_position_id,
  approximate
FROM (
  SELECT p.user_id AS user_id, c.unit_id AS unit_id, p.id AS position_id,
    ROW_NUMBER() OVER (
      PARTITION BY p.user_id, c.unit_id ORDER BY p.server_ts DESC, p.id DESC
    ) AS rn,
    CASE WHEN c.start_page IS NOT NULL AND c.end_page IS NOT NULL AND p.page IS NOT NULL
      THEN p.page >= c.end_page
      ELSE coalesce(p.percent, 0) >= (c.rank0 + 1) * 1.0 / c.n END AS done,
    CASE WHEN c.start_page IS NOT NULL AND c.end_page IS NOT NULL AND p.page IS NOT NULL
      THEN p.page >= c.start_page
      ELSE coalesce(p.percent, 0) > c.rank0 * 1.0 / c.n END AS started,
    CASE WHEN c.start_page IS NOT NULL AND c.end_page IS NOT NULL AND p.page IS NOT NULL
      THEN 0 ELSE c.n > 1 OR p.percent IS NULL END AS approximate
  FROM (
    SELECT id, user_id, carrier_type, carrier_id, percent, server_ts,
      json_extract(locator, '$.page') AS page
    FROM reading_position
  ) AS p
  JOIN (
    SELECT carrier_type, carrier_id, unit_id,
      json_extract(start_locator, '$.page') AS start_page,
      json_extract(end_locator, '$.page') AS end_page,
      ROW_NUMBER() OVER (PARTITION BY carrier_type, carrier_id ORDER BY position) - 1 AS rank0,
      COUNT(*) OVER (PARTITION BY carrier_type, carrier_id) AS n
    FROM unit_coverage
  ) AS c ON c.carrier_type = p.carrier_type AND c.carrier_id = p.carrier_id
) WHERE rn = 1
"""

# Rebuilding `unit_variant` drops the table the append-only guard of 0003 reads; the guard goes
# away and comes back around the rebuild. Copy of the statement of 0003.
VARIANT_REVISION_NO_DELETE = (
    "CREATE TRIGGER variant_revision_no_delete "
    "BEFORE DELETE ON variant_revision "
    "WHEN EXISTS (SELECT 1 FROM unit_variant WHERE id = OLD.variant_id) "
    "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END"
)


def _now(name: str) -> sa.Column[datetime]:
    """Batch mode re-renders a reflected `CURRENT_TIMESTAMP` default without its parentheses."""
    return sa.Column(
        name, sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )


def _blank(name: str) -> sa.Column[str]:
    """The `''` sentinel of the Edition key, as the metadata declares it."""
    return sa.Column(name, sa.String(), server_default=sa.text("''"), nullable=False)


def _rebuild(table: str) -> AbstractContextManager[BatchOperations]:
    """Rebuild an existing table in batch mode, keeping its defaults as the metadata spells them.

    Reflection hands back `CURRENT_TIMESTAMP` and `''` defaults in a form that SQLite re-renders
    with parentheses, so those columns are restated.
    """
    restated: dict[str, list[sa.Column[Any]]] = {
        "edition": [
            _now("created_at"),
            *(_blank(name) for name in ("publisher", "text_version", "performance", "packaging")),
        ],
        "export_plan": [_now("created_at")],
        "physical_root": [_now("created_at")],
        "unit_variant": [_now("first_seen_at")],
    }
    return op.batch_alter_table(table, recreate="always", reflect_args=tuple(restated[table]))


def upgrade() -> None:
    op.create_table(
        "shelf",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("phrase", sa.String(), nullable=False),
        sa.Column("default_kinds", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("format_policy", sa.String(), server_default="cbz_per_chapter", nullable=False),
        sa.Column("library_id", sa.String(), nullable=True),
        sa.Column("max_age_rating", sa.Integer(), nullable=True),
        sa.Column("allowed_profiles", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("retention_mode", sa.String(), server_default="passthrough", nullable=False),
        sa.Column(
            "purge_upstream_after_export", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("hold_until_rated", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("paused_reason", sa.String(), nullable=True),
        sa.Column("follow_suspended", sa.JSON(), server_default="[]", nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "\"format_policy\" IN ('cbz_per_chapter', 'per_volume')",
            name=op.f("ck_shelf_format_policy_values"),
        ),
        sa.CheckConstraint(
            '"max_age_rating" IS NULL OR (typeof("max_age_rating") = \'integer\' AND "max_age_rating" BETWEEN 0 AND 99)',
            name=op.f("ck_shelf_max_age_rating_min_age"),
        ),
        sa.CheckConstraint("\"name\" <> ''", name=op.f("ck_shelf_name_not_empty")),
        sa.CheckConstraint(
            "\"retention_mode\" IN ('passthrough', 'keep')",
            name=op.f("ck_shelf_retention_mode_values"),
        ),
        sa.CheckConstraint(
            'json_valid("allowed_profiles") AND json_type("allowed_profiles") = \'array\'',
            name=op.f("ck_shelf_allowed_profiles_list"),
        ),
        sa.CheckConstraint(
            'json_valid("default_kinds") AND json_type("default_kinds") = \'array\'',
            name=op.f("ck_shelf_default_kinds_list"),
        ),
        sa.CheckConstraint(
            'json_valid("follow_suspended") AND json_type("follow_suspended") = \'array\'',
            name=op.f("ck_shelf_follow_suspended_list"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_shelf_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shelf")),
        sa.UniqueConstraint("name", name=op.f("uq_shelf_name")),
    )
    op.create_table(
        "export_target",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("access_partitioning", sa.String(), server_default="none", nullable=False),
        sa.Column("access_partitioning_attested_at", sa.DateTime(), nullable=True),
        sa.Column("config", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("secret_kid", sa.String(), nullable=True),
        sa.Column("secret_v", sa.Integer(), nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("secret_ct", sa.LargeBinary(), nullable=True),
        sa.Column("path_map", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("version_seen", sa.String(), nullable=True),
        sa.Column("health", sa.JSON(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "\"access_partitioning\" IN ('none', 'library', 'user')",
            name=op.f("ck_export_target_access_partitioning_values"),
        ),
        sa.CheckConstraint(
            "\"kind\" <> '' AND \"kind\" NOT GLOB '*[^a-z0-9_]*'",
            name=op.f("ck_export_target_kind_slug"),
        ),
        sa.CheckConstraint("\"name\" <> ''", name=op.f("ck_export_target_name_not_empty")),
        sa.CheckConstraint(
            '("secret_kid" IS NULL AND "secret_v" IS NULL AND "secret_nonce" IS NULL AND "secret_ct" IS NULL) OR ("secret_kid" IS NOT NULL AND "secret_v" IS NOT NULL AND "secret_v" >= 1 AND "secret_nonce" IS NOT NULL AND "secret_ct" IS NOT NULL AND length("secret_ct") >= 16)',
            name=op.f("ck_export_target_secret_sealed"),
        ),
        sa.CheckConstraint(
            'json_valid("config") AND json_type("config") = \'object\'',
            name=op.f("ck_export_target_config_object"),
        ),
        sa.CheckConstraint(
            'json_valid("path_map") AND json_type("path_map") = \'array\'',
            name=op.f("ck_export_target_path_map_list"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_export_target_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["secret_kid"], ["secret_key.kid"], name=op.f("fk_export_target_secret_kid_secret_key")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_target")),
        sa.UniqueConstraint("name", name=op.f("uq_export_target_name")),
    )
    op.create_table(
        "reading_position",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("carrier_type", sa.String(), nullable=False),
        sa.Column("carrier_id", sa.String(length=26), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("locator", sa.JSON(), nullable=False),
        sa.Column("percent", sa.Float(), nullable=True),
        sa.Column("client_ts", sa.DateTime(), nullable=False),
        sa.Column(
            "server_ts", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("device_id", sa.String(), nullable=True),
        sa.CheckConstraint(
            "\"carrier_type\" IN ('export_item', 'content')",
            name=op.f("ck_reading_position_carrier_type_values"),
        ),
        sa.CheckConstraint(
            '"percent" IS NULL OR "percent" BETWEEN 0 AND 1',
            name=op.f("ck_reading_position_percent_range"),
        ),
        sa.CheckConstraint(
            'json_valid("locator") AND json_type("locator") = \'object\'',
            name=op.f("ck_reading_position_locator_object"),
        ),
        sa.CheckConstraint(
            'length("carrier_id") = 26 AND "carrier_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_reading_position_carrier_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("content_hash") = 71 AND "content_hash" GLOB \'sha256:*\' AND substr("content_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_reading_position_content_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_reading_position_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_reading_position_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reading_position")),
    )
    op.create_index(
        "ix_reading_position_content_hash", "reading_position", ["content_hash"], unique=False
    )
    op.create_index(
        "ix_reading_position_user",
        "reading_position",
        ["user_id", "carrier_type", "carrier_id", "server_ts"],
        unique=False,
    )
    op.create_table(
        "trash_item",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("ref", sa.String(length=26), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("physical_root_id", sa.String(length=26), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(), nullable=False),
        sa.Column("purge_after", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            '"kind" <> \'export_file\' OR "physical_root_id" IS NOT NULL',
            name=op.f("ck_trash_item_export_file_has_root"),
        ),
        sa.CheckConstraint(
            "\"kind\" IN ('unit', 'work', 'export_file')", name=op.f("ck_trash_item_kind_values")
        ),
        sa.CheckConstraint(
            "\"path\" NOT GLOB '/*' AND \"path\" <> '..' AND \"path\" NOT GLOB '../*' AND \"path\" NOT GLOB '*/../*' AND \"path\" NOT GLOB '*/..'",
            name=op.f("ck_trash_item_path_relative"),
        ),
        sa.CheckConstraint(
            '"purge_after" >= "deleted_at"', name=op.f("ck_trash_item_purge_after_deleted_at")
        ),
        sa.CheckConstraint('"size" >= 0', name=op.f("ck_trash_item_size_min")),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_trash_item_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("ref") = 26 AND "ref" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_trash_item_ref_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["physical_root_id"],
            ["physical_root.root_id"],
            name=op.f("fk_trash_item_physical_root_id_physical_root"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_trash_item")),
    )
    op.create_index("ix_trash_item_purge_after", "trash_item", ["purge_after"], unique=False)
    op.create_index("ix_trash_item_ref", "trash_item", ["kind", "ref"], unique=False)
    op.create_table(
        "kavita_instance",
        sa.Column("target_id", sa.String(length=26), nullable=False),
        sa.Column("fingerprint", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["export_target.id"],
            name=op.f("fk_kavita_instance_target_id_export_target"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("target_id", name=op.f("pk_kavita_instance")),
    )
    op.create_table(
        "remote_library",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("export_target_id", sa.String(length=26), nullable=False),
        sa.Column("remote_ref", sa.String(), nullable=False),
        sa.Column("kind_seen", sa.String(), nullable=True),
        sa.Column("seen_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "\"remote_ref\" <> ''", name=op.f("ck_remote_library_remote_ref_not_empty")
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_remote_library_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["export_target_id"],
            ["export_target.id"],
            name=op.f("fk_remote_library_export_target_id_export_target"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_remote_library")),
        sa.UniqueConstraint(
            "export_target_id",
            "remote_ref",
            name=op.f("uq_remote_library_export_target_id_remote_ref"),
        ),
        sa.UniqueConstraint(
            "id", "export_target_id", name=op.f("uq_remote_library_id_export_target_id")
        ),
    )
    op.create_table(
        "shelf_destination",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("shelf_id", sa.String(length=26), nullable=False),
        sa.Column("target_id", sa.String(length=26), nullable=False),
        sa.Column("transport", sa.String(), nullable=False),
        sa.Column("egress", sa.String(), nullable=False),
        sa.Column("root_subpath", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("template_origin", sa.String(), nullable=False),
        sa.Column("import_mode", sa.String(), server_default="copy", nullable=False),
        sa.Column("kavita_library_type", sa.String(), nullable=True),
        sa.Column("mode", sa.String(), server_default="normal", nullable=False),
        sa.Column("adopted", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("pre_adoption_settings", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("sync_progress", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "kavita_series_age_locked", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "\"egress\" IN ('foyer', 'externe')", name=op.f("ck_shelf_destination_egress_values")
        ),
        sa.CheckConstraint(
            "\"import_mode\" IN ('copy', 'reflink', 'hardlink')",
            name=op.f("ck_shelf_destination_import_mode_values"),
        ),
        sa.CheckConstraint(
            "\"mode\" IN ('normal', 'degraded_readonly', 'degraded_feature')",
            name=op.f("ck_shelf_destination_mode_values"),
        ),
        sa.CheckConstraint(
            "\"template\" <> '' AND \"template_origin\" <> ''",
            name=op.f("ck_shelf_destination_template_not_empty"),
        ),
        sa.CheckConstraint(
            "\"transport\" IN ('tree', 'dropbox', 'push', 'pull')",
            name=op.f("ck_shelf_destination_transport_values"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_shelf_destination_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["shelf_id"], ["shelf.id"], name=op.f("fk_shelf_destination_shelf_id_shelf")
        ),
        sa.ForeignKeyConstraint(
            ["target_id"],
            ["export_target.id"],
            name=op.f("fk_shelf_destination_target_id_export_target"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shelf_destination")),
        sa.UniqueConstraint("id", "target_id", name=op.f("uq_shelf_destination_id_target_id")),
        sa.UniqueConstraint(
            "shelf_id",
            "target_id",
            "root_subpath",
            name=op.f("uq_shelf_destination_shelf_id_target_id_root_subpath"),
        ),
    )
    op.create_index("ix_shelf_destination_target", "shelf_destination", ["target_id"], unique=False)
    op.create_table(
        "kavita_access_audit",
        sa.Column("remote_library_id", sa.String(length=26), nullable=False),
        sa.Column("accounts", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("age_restriction", sa.Integer(), nullable=True),
        sa.Column("include_unknowns", sa.Boolean(), nullable=True),
        sa.Column("exposed_to_children", sa.Boolean(), nullable=False),
        sa.Column("kavita_version", sa.String(), nullable=True),
        sa.Column("audited_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            '"age_restriction" IS NULL OR (typeof("age_restriction") = \'integer\' AND "age_restriction" BETWEEN 0 AND 99)',
            name=op.f("ck_kavita_access_audit_age_restriction_min_age"),
        ),
        sa.CheckConstraint(
            'json_valid("accounts") AND json_type("accounts") = \'array\'',
            name=op.f("ck_kavita_access_audit_accounts_list"),
        ),
        sa.ForeignKeyConstraint(
            ["remote_library_id"],
            ["remote_library.id"],
            name=op.f("fk_kavita_access_audit_remote_library_id_remote_library"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("remote_library_id", name=op.f("pk_kavita_access_audit")),
    )
    op.create_table(
        "shelf_destination_remote_library",
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=False),
        sa.Column("remote_library_id", sa.String(length=26), nullable=False),
        sa.Column("export_target_id", sa.String(length=26), nullable=False),
        sa.ForeignKeyConstraint(
            ["remote_library_id", "export_target_id"],
            ["remote_library.id", "remote_library.export_target_id"],
            name=op.f(
                "fk_shelf_destination_remote_library_remote_library_id_export_target_id_remote_library"
            ),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shelf_destination_id", "export_target_id"],
            ["shelf_destination.id", "shelf_destination.target_id"],
            name=op.f(
                "fk_shelf_destination_remote_library_shelf_destination_id_export_target_id_shelf_destination"
            ),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "shelf_destination_id",
            "remote_library_id",
            name=op.f("pk_shelf_destination_remote_library"),
        ),
    )
    op.create_index(
        "ix_shelf_destination_remote_library_library",
        "shelf_destination_remote_library",
        ["remote_library_id"],
        unique=False,
    )
    op.create_table(
        "export_series",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=False),
        sa.Column("remote_library_id", sa.String(length=26), nullable=False),
        sa.Column("proposal_key", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("normalized_name", sa.String(), nullable=False),
        sa.Column("name_norm_by", sa.String(), nullable=True),
        sa.Column("name_raw", sa.String(), nullable=False),
        sa.Column("kavita_series_id", sa.String(), nullable=True),
        sa.Column("frozen_series", sa.String(), nullable=True),
        sa.Column("frozen_localized_series", sa.String(), nullable=True),
        sa.Column("frozen_series_sort", sa.String(), nullable=True),
        sa.Column("frozen_language", sa.String(), nullable=True),
        sa.Column("has_volume", sa.Boolean(), nullable=True),
        sa.Column("series_dir", sa.String(), nullable=True),
        sa.Column("frozen_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            '"name_norm_by" IS NULL OR ("name_norm_by" GLOB \'?*@[0-9]*\' AND "name_norm_by" NOT GLOB \'*@*[^0-9]*\' AND "name_norm_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_export_series_name_norm_by_versioned"),
        ),
        sa.CheckConstraint(
            "\"normalized_name\" <> ''", name=op.f("ck_export_series_normalized_name_not_empty")
        ),
        sa.CheckConstraint(
            '("frozen_series" IS NULL) = ("frozen_at" IS NULL)',
            name=op.f("ck_export_series_frozen_with_date"),
        ),
        sa.CheckConstraint(
            "length(\"frozen_language\") > 0 AND \"frozen_language\" NOT GLOB '*[^A-Za-z0-9-]*' AND \"frozen_language\" NOT GLOB '-*' AND \"frozen_language\" NOT GLOB '*-' AND lower(\"frozen_language\") NOT IN ('all', 'multi', 'und', 'mul')",
            name=op.f("ck_export_series_frozen_language"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_export_series_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["shelf_destination_id", "remote_library_id"],
            [
                "shelf_destination_remote_library.shelf_destination_id",
                "shelf_destination_remote_library.remote_library_id",
            ],
            name=op.f(
                "fk_export_series_shelf_destination_id_remote_library_id_shelf_destination_remote_library"
            ),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_series")),
        sa.UniqueConstraint(
            "id", "remote_library_id", name=op.f("uq_export_series_id_remote_library_id")
        ),
        sa.UniqueConstraint(
            "id", "shelf_destination_id", name=op.f("uq_export_series_id_shelf_destination_id")
        ),
        sa.UniqueConstraint(
            "remote_library_id",
            "normalized_name",
            name=op.f("uq_export_series_remote_library_id_normalized_name"),
        ),
    )
    op.create_table(
        "edition_export_series",
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("remote_library_id", sa.String(length=26), nullable=False),
        sa.Column("export_series_id", sa.String(length=26), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=False),
        sa.Column("assigned_by", sa.String(), nullable=False),
        sa.CheckConstraint(
            "\"assigned_by\" IN ('first_export', 'plan')",
            name=op.f("ck_edition_export_series_assigned_by_values"),
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["edition.id"], name=op.f("fk_edition_export_series_edition_id_edition")
        ),
        sa.ForeignKeyConstraint(
            ["export_series_id", "remote_library_id"],
            ["export_series.id", "export_series.remote_library_id"],
            name=op.f("fk_edition_export_series_export_series_id_remote_library_id_export_series"),
        ),
        sa.ForeignKeyConstraint(
            ["remote_library_id"],
            ["remote_library.id"],
            name=op.f("fk_edition_export_series_remote_library_id_remote_library"),
        ),
        sa.PrimaryKeyConstraint(
            "edition_id", "remote_library_id", name=op.f("pk_edition_export_series")
        ),
    )
    op.create_index(
        "ix_edition_export_series_series",
        "edition_export_series",
        ["export_series_id"],
        unique=False,
    )
    op.create_table(
        "field_owner",
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=False),
        sa.Column("export_series_id", sa.String(length=26), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("last_value", sa.JSON(), nullable=False),
        sa.Column("pushed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint("\"field\" <> ''", name=op.f("ck_field_owner_field_not_empty")),
        sa.ForeignKeyConstraint(
            ["export_series_id", "shelf_destination_id"],
            ["export_series.id", "export_series.shelf_destination_id"],
            name=op.f("fk_field_owner_export_series_id_shelf_destination_id_export_series"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "shelf_destination_id", "export_series_id", "field", name=op.f("pk_field_owner")
        ),
    )
    op.create_table(
        "unit_coverage",
        sa.Column("carrier_type", sa.String(), nullable=False),
        sa.Column("carrier_id", sa.String(length=26), nullable=False),
        sa.Column("unit_id", sa.String(length=26), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("start_locator", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("end_locator", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("extent", sa.String(), server_default="full", nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.CheckConstraint(
            "\"carrier_type\" IN ('content', 'export_item', 'volume', 'unit')",
            name=op.f("ck_unit_coverage_carrier_type_values"),
        ),
        sa.CheckConstraint(
            '"confidence" IS NULL OR "confidence" BETWEEN 0 AND 1',
            name=op.f("ck_unit_coverage_confidence_range"),
        ),
        sa.CheckConstraint(
            "\"extent\" IN ('full', 'partial')", name=op.f("ck_unit_coverage_extent_values")
        ),
        sa.CheckConstraint(
            "\"origin\" IN ('comicinfo', 'filename', 'source_name', 'provider', 'manual')",
            name=op.f("ck_unit_coverage_origin_values"),
        ),
        sa.CheckConstraint('"position" >= 0', name=op.f("ck_unit_coverage_position_min")),
        sa.CheckConstraint(
            '"start_locator" IS NULL OR json_valid("start_locator")',
            name=op.f("ck_unit_coverage_start_locator_json"),
        ),
        sa.CheckConstraint(
            '"end_locator" IS NULL OR json_valid("end_locator")',
            name=op.f("ck_unit_coverage_end_locator_json"),
        ),
        sa.CheckConstraint(
            'length("carrier_id") = 26 AND "carrier_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_unit_coverage_carrier_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"], ["unit.id"], name=op.f("fk_unit_coverage_unit_id_unit"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "carrier_type", "carrier_id", "unit_id", name=op.f("pk_unit_coverage")
        ),
        sa.UniqueConstraint(
            "carrier_type",
            "carrier_id",
            "position",
            name=op.f("uq_unit_coverage_carrier_type_carrier_id_position"),
        ),
    )
    op.create_index("ix_unit_coverage_unit", "unit_coverage", ["unit_id"], unique=False)
    op.create_table(
        "content",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("unit_id", sa.String(length=26), nullable=True),
        sa.Column("variant_id", sa.String(length=26), nullable=True),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("media_type", sa.String(), nullable=False),
        sa.Column("format", sa.String(), nullable=False),
        sa.Column("watermarked", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("retention", sa.String(), nullable=False),
        sa.Column("path", sa.Text(), nullable=True),
        sa.Column("original_sha256", sa.String(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("content_hash_by", sa.String(), nullable=True),
        sa.Column("store_provenance", sa.String(), nullable=False),
        sa.Column("derived_hash", sa.String(), nullable=True),
        sa.Column("derived_by", sa.String(), nullable=True),
        sa.Column("derived_hash_by", sa.String(), nullable=True),
        sa.Column("derived_chain", sa.JSON(), server_default="[]", nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            '"content_hash_by" IS NULL OR ("content_hash_by" GLOB \'?*@[0-9]*\' AND "content_hash_by" NOT GLOB \'*@*[^0-9]*\' AND "content_hash_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_content_content_hash_by_versioned"),
        ),
        sa.CheckConstraint(
            '"derived_by" IS NULL OR ("derived_by" GLOB \'?*@?*\' AND "derived_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_content_derived_by_versioned"),
        ),
        sa.CheckConstraint(
            '"derived_hash_by" IS NULL OR ("derived_hash_by" GLOB \'?*@[0-9]*\' AND "derived_hash_by" NOT GLOB \'*@*[^0-9]*\' AND "derived_hash_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_content_derived_hash_by_versioned"),
        ),
        sa.CheckConstraint(
            "\"format\" <> '' AND \"format\" NOT GLOB '*[^a-z0-9]*'",
            name=op.f("ck_content_format_slug"),
        ),
        sa.CheckConstraint(
            "\"kind\" IN ('images', 'text', 'file', 'audio')", name=op.f("ck_content_kind_values")
        ),
        sa.CheckConstraint("\"media_type\" GLOB '?*/?*'", name=op.f("ck_content_media_type_mime")),
        sa.CheckConstraint('"page_count" >= 0', name=op.f("ck_content_page_count_min")),
        sa.CheckConstraint(
            "\"path\" NOT GLOB '/*' AND \"path\" <> '..' AND \"path\" NOT GLOB '../*' AND \"path\" NOT GLOB '*/../*' AND \"path\" NOT GLOB '*/..'",
            name=op.f("ck_content_path_relative"),
        ),
        sa.CheckConstraint(
            "\"retention\" IN ('passthrough', 'keep', 'adopted_ref')",
            name=op.f("ck_content_retention_values"),
        ),
        sa.CheckConstraint('"size" >= 0', name=op.f("ck_content_size_min")),
        sa.CheckConstraint(
            "\"store_provenance\" IN ('received', 'extracted', 'extracted_derived')",
            name=op.f("ck_content_store_provenance_values"),
        ),
        sa.CheckConstraint(
            '"variant_id" IS NOT NULL OR "retention" = \'adopted_ref\'',
            name=op.f("ck_content_variant_unless_adopted"),
        ),
        sa.CheckConstraint(
            '("derived_hash" IS NULL) = ("derived_by" IS NULL)',
            name=op.f("ck_content_derived_pair"),
        ),
        sa.CheckConstraint(
            '"store_provenance" <> \'extracted_derived\' OR "derived_hash" IS NOT NULL',
            name=op.f("ck_content_extracted_derived_has_derived_hash"),
        ),
        sa.CheckConstraint(
            '("retention" = \'keep\') = ("path" IS NOT NULL)', name=op.f("ck_content_path_iff_keep")
        ),
        sa.CheckConstraint(
            'json_valid("derived_chain") AND json_type("derived_chain") = \'array\'',
            name=op.f("ck_content_derived_chain_list"),
        ),
        sa.CheckConstraint(
            'length("content_hash") = 71 AND "content_hash" GLOB \'sha256:*\' AND substr("content_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_content_content_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("derived_hash") = 71 AND "derived_hash" GLOB \'sha256:*\' AND substr("derived_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_content_derived_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_content_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("original_sha256") = 71 AND "original_sha256" GLOB \'sha256:*\' AND substr("original_sha256", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_content_original_sha256_sha256_prefixed"),
        ),
        sa.ForeignKeyConstraint(["unit_id"], ["unit.id"], name=op.f("fk_content_unit_id_unit")),
        sa.ForeignKeyConstraint(
            ["variant_id", "unit_id"],
            ["unit_variant.id", "unit_variant.unit_id"],
            name=op.f("fk_content_variant_id_unit_id_unit_variant"),
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"], ["unit_variant.id"], name=op.f("fk_content_variant_id_unit_variant")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_content")),
        sa.UniqueConstraint(
            "variant_id", "media_type", name=op.f("uq_content_variant_id_media_type")
        ),
    )
    op.create_index("ix_content_content_hash", "content", ["content_hash"], unique=False)
    op.create_index("ix_content_unit", "content", ["unit_id"], unique=False)
    op.create_table(
        "export_item",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("frozen_attrs", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("closed", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("content_id", sa.String(length=26), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "\"kind\" IN ('unit', 'volume', 'bundle', 'adopted')",
            name=op.f("ck_export_item_kind_values"),
        ),
        sa.CheckConstraint(
            'json_valid("frozen_attrs") AND json_type("frozen_attrs") = \'object\'',
            name=op.f("ck_export_item_frozen_attrs_object"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_export_item_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["content_id"],
            ["content.id"],
            name=op.f("fk_export_item_content_id_content"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["edition.id"], name=op.f("fk_export_item_edition_id_edition")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_export_item")),
    )
    op.create_index("ix_export_item_content", "export_item", ["content_id"], unique=False)
    op.create_index("ix_export_item_edition", "export_item", ["edition_id"], unique=False)
    op.create_table(
        "document_alias",
        sa.Column("export_item_id", sa.String(length=26), nullable=False),
        sa.Column("file_hash", sa.String(), nullable=False),
        sa.Column("koreader_partial_md5", sa.String(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            'length("content_hash") = 71 AND "content_hash" GLOB \'sha256:*\' AND substr("content_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_document_alias_content_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("file_hash") = 71 AND "file_hash" GLOB \'sha256:*\' AND substr("file_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_document_alias_file_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("koreader_partial_md5") = 36 AND "koreader_partial_md5" GLOB \'md5:*\' AND substr("koreader_partial_md5", 5) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_document_alias_koreader_partial_md5_md5_prefixed"),
        ),
        sa.ForeignKeyConstraint(
            ["export_item_id"],
            ["export_item.id"],
            name=op.f("fk_document_alias_export_item_id_export_item"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("export_item_id", "file_hash", name=op.f("pk_document_alias")),
    )
    op.create_index("ix_document_alias_file_hash", "document_alias", ["file_hash"], unique=False)
    op.create_index(
        "ix_document_alias_md5", "document_alias", ["koreader_partial_md5"], unique=False
    )
    op.create_table(
        "export_record",
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=False),
        sa.Column("export_item_id", sa.String(length=26), nullable=False),
        sa.Column("locator_type", sa.String(), nullable=False),
        sa.Column("rel_path", sa.Text(), nullable=True),
        sa.Column("remote_id", sa.String(), nullable=True),
        sa.Column("idempotency_key", sa.String(), nullable=True),
        sa.Column("template_ref", sa.String(), nullable=True),
        sa.Column("format", sa.String(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("sidecar_sha256", sa.String(), nullable=True),
        sa.Column("file_hash", sa.String(), nullable=True),
        sa.Column("remote_ref", sa.String(), nullable=True),
        sa.Column("written_by_shelfhold_at", sa.DateTime(), nullable=True),
        sa.Column("exported_at", sa.DateTime(), nullable=True),
        sa.Column("state", sa.String(), server_default="planned", nullable=False),
        sa.Column("error_code", sa.String(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "\"locator_type\" IN ('path', 'remote_id', 'idempotency_key', 'none')",
            name=op.f("ck_export_record_locator_type_values"),
        ),
        sa.CheckConstraint(
            "\"rel_path\" NOT GLOB '/*' AND \"rel_path\" <> '..' AND \"rel_path\" NOT GLOB '../*' AND \"rel_path\" NOT GLOB '*/../*' AND \"rel_path\" NOT GLOB '*/..'",
            name=op.f("ck_export_record_rel_path_relative"),
        ),
        sa.CheckConstraint('"size" >= 0', name=op.f("ck_export_record_size_min")),
        sa.CheckConstraint(
            "\"state\" IN ('planned', 'written', 'scanned', 'resolved', 'adopted', 'missing', 'recycled', 'error')",
            name=op.f("ck_export_record_state_values"),
        ),
        sa.CheckConstraint(
            '("locator_type" = \'path\' AND "rel_path" IS NOT NULL AND "remote_id" IS NULL AND "idempotency_key" IS NULL) OR ("locator_type" = \'remote_id\' AND "remote_id" IS NOT NULL AND "rel_path" IS NULL AND "idempotency_key" IS NULL) OR ("locator_type" = \'idempotency_key\' AND "idempotency_key" IS NOT NULL AND "rel_path" IS NULL AND "remote_id" IS NULL) OR ("locator_type" = \'none\' AND "rel_path" IS NULL AND "remote_id" IS NULL AND "idempotency_key" IS NULL)',
            name=op.f("ck_export_record_locator_coherent"),
        ),
        sa.CheckConstraint(
            'length("content_hash") = 71 AND "content_hash" GLOB \'sha256:*\' AND substr("content_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_export_record_content_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("file_hash") = 71 AND "file_hash" GLOB \'sha256:*\' AND substr("file_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_export_record_file_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("sidecar_sha256") = 71 AND "sidecar_sha256" GLOB \'sha256:*\' AND substr("sidecar_sha256", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_export_record_sidecar_sha256_sha256_prefixed"),
        ),
        sa.ForeignKeyConstraint(
            ["export_item_id"],
            ["export_item.id"],
            name=op.f("fk_export_record_export_item_id_export_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["shelf_destination_id"],
            ["shelf_destination.id"],
            name=op.f("fk_export_record_shelf_destination_id_shelf_destination"),
        ),
        sa.PrimaryKeyConstraint(
            "shelf_destination_id", "export_item_id", name=op.f("pk_export_record")
        ),
    )
    op.create_index("ix_export_record_file_hash", "export_record", ["file_hash"], unique=False)
    op.create_index("ix_export_record_item", "export_record", ["export_item_id"], unique=False)
    op.create_index(
        "uq_export_record_path",
        "export_record",
        ["shelf_destination_id", "rel_path"],
        unique=True,
        sqlite_where=sa.text('"rel_path" IS NOT NULL AND "state" <> \'recycled\''),
    )
    op.create_table(
        "replica",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("unit_id", sa.String(length=26), nullable=True),
        sa.Column("export_item_id", sa.String(length=26), nullable=True),
        sa.Column("location", sa.String(), nullable=False),
        sa.Column("physical_root_id", sa.String(length=26), nullable=True),
        sa.Column("rel_path", sa.Text(), nullable=True),
        sa.Column("remote_ref", sa.String(), nullable=True),
        sa.Column("dev", sa.BigInteger(), nullable=True),
        sa.Column("ino", sa.BigInteger(), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("mtime_ns", sa.BigInteger(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("file_hash", sa.String(), nullable=True),
        sa.Column("independent", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "mutable_by_third_party", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column("fidelity", sa.String(), server_default="original", nullable=False),
        sa.Column("verified_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            "\"fidelity\" IN ('original', 'derived')", name=op.f("ck_replica_fidelity_values")
        ),
        sa.CheckConstraint(
            "\"location\" IN ('upstream', 'library', 'store', 'destination', 'adopted_ro')",
            name=op.f("ck_replica_location_values"),
        ),
        sa.CheckConstraint(
            '"location" NOT IN (\'library\', \'adopted_ro\') OR ("physical_root_id" IS NOT NULL AND "rel_path" IS NOT NULL)',
            name=op.f("ck_replica_library_has_root_and_path"),
        ),
        sa.CheckConstraint(
            '"rel_path" IS NOT NULL OR "remote_ref" IS NOT NULL', name=op.f("ck_replica_is_located")
        ),
        sa.CheckConstraint('"size" >= 0', name=op.f("ck_replica_size_min")),
        sa.CheckConstraint(
            '"unit_id" IS NOT NULL OR "export_item_id" IS NOT NULL',
            name=op.f("ck_replica_has_owner"),
        ),
        sa.CheckConstraint(
            "\"rel_path\" NOT GLOB '/*' AND \"rel_path\" <> '..' AND \"rel_path\" NOT GLOB '../*' AND \"rel_path\" NOT GLOB '*/../*' AND \"rel_path\" NOT GLOB '*/..'",
            name=op.f("ck_replica_rel_path_relative"),
        ),
        sa.CheckConstraint(
            'length("content_hash") = 71 AND "content_hash" GLOB \'sha256:*\' AND substr("content_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_replica_content_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("file_hash") = 71 AND "file_hash" GLOB \'sha256:*\' AND substr("file_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_replica_file_hash_sha256_prefixed"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_replica_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["export_item_id"],
            ["export_item.id"],
            name=op.f("fk_replica_export_item_id_export_item"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["physical_root_id"],
            ["physical_root.root_id"],
            name=op.f("fk_replica_physical_root_id_physical_root"),
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"], ["unit.id"], name=op.f("fk_replica_unit_id_unit"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_replica")),
    )
    op.create_index("ix_replica_content_hash", "replica", ["content_hash"], unique=False)
    op.create_index("ix_replica_export_item", "replica", ["export_item_id"], unique=False)
    op.create_index("ix_replica_inode", "replica", ["dev", "ino"], unique=False)
    op.create_index("ix_replica_unit", "replica", ["unit_id"], unique=False)
    op.create_index(
        "uq_replica_root_path",
        "replica",
        ["physical_root_id", "rel_path"],
        unique=True,
        sqlite_where=sa.text('"physical_root_id" IS NOT NULL AND "rel_path" IS NOT NULL'),
    )
    op.create_index(
        "uq_replica_store_path",
        "replica",
        ["rel_path"],
        unique=True,
        sqlite_where=sa.text("\"location\" = 'store'"),
    )

    for statement in TRIGGERS.values():
        op.execute(statement)
    op.execute(f"CREATE VIEW reading_progress AS {READING_PROGRESS}")

    with _rebuild("edition") as batch:
        batch.add_column(sa.Column("shelf_id", sa.String(length=26), nullable=True))
        batch.create_index("ix_edition_shelf", ["shelf_id"], unique=False)
        batch.create_foreign_key(
            op.f("fk_edition_shelf_id_shelf"), "shelf", ["shelf_id"], ["id"], ondelete="SET NULL"
        )

    with _rebuild("export_plan") as batch:
        batch.create_foreign_key(
            op.f("fk_export_plan_shelf_destination_id_shelf_destination"),
            "shelf_destination",
            ["shelf_destination_id"],
            ["id"],
            ondelete="SET NULL",
        )

    with _rebuild("physical_root") as batch:
        batch.add_column(sa.Column("mode", sa.String(), server_default="rw", nullable=False))
        batch.create_check_constraint(
            op.f("ck_physical_root_mode_values"), "\"mode\" IN ('rw', 'adopt_ro')"
        )

    op.execute("DROP TRIGGER variant_revision_no_delete")
    with _rebuild("unit_variant") as batch:
        batch.create_unique_constraint(op.f("uq_unit_variant_id_unit_id"), ["id", "unit_id"])
    op.execute(VARIANT_REVISION_NO_DELETE)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS reading_progress")
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")

    op.execute("DROP TRIGGER variant_revision_no_delete")
    with _rebuild("unit_variant") as batch:
        batch.drop_constraint(op.f("uq_unit_variant_id_unit_id"), type_="unique")
    op.execute(VARIANT_REVISION_NO_DELETE)

    with _rebuild("physical_root") as batch:
        batch.drop_constraint(op.f("ck_physical_root_mode_values"), type_="check")
        batch.drop_column("mode")

    with _rebuild("export_plan") as batch:
        batch.drop_constraint(
            op.f("fk_export_plan_shelf_destination_id_shelf_destination"), type_="foreignkey"
        )

    with _rebuild("edition") as batch:
        batch.drop_constraint(op.f("fk_edition_shelf_id_shelf"), type_="foreignkey")
        batch.drop_index("ix_edition_shelf")
        batch.drop_column("shelf_id")

    for table in TABLES:  # children first; indexes go with their table
        op.drop_table(table)
