"""M1a schema, catalog and acquisition (PLAN-M1a L03).

Tables only: storage / export / reading arrive in 0004. The revision is a frozen snapshot: it does
not import the live metadata, and `test_metadata_matches_migrations` and
`test_migrated_ddl_equals_create_all` keep the two honest.
"""

# ruff: noqa: E501  (frozen, generated constraint expressions)

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

TABLES = (
    "work_relation",
    "work_relation_kind",
    "download_request_unit",
    "variant_revision",
    "revision_pending",
    "download_request",
    "unit_variant",
    "unit",
    "team_priority",
    "team_alias",
    "source_failure",
    "volume",
    "team",
    "source_binding",
    "series_follow",
    "gap_ack",
    "work_title",
    "series_member",
    "edition",
    "work",
    "source_config",
    "source_account",
    "contributor_name",
    "contribution",
    "work_kind",
    "upstream_snapshot",
    "sub_source",
    "source_item",
    "source_alias",
    "series",
    "plugin_state",
    "plugin_install",
    "normalizer",
    "merge_suggestion",
    "external_id",
    "contributor",
    "age_suggestion",
)

# Append-only guard on the revision history. Statements are copies of the ones in
# infrastructure/schema/catalog.py. A direct delete is refused while the Variant lives; the
# cascade of an erasure (EF-BIB-052) runs once the Variant row is gone.
TRIGGERS = {
    "variant_revision_no_update": (
        "CREATE TRIGGER variant_revision_no_update "
        "BEFORE UPDATE ON variant_revision "
        "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END"
    ),
    "variant_revision_no_replace": (
        "CREATE TRIGGER variant_revision_no_replace "
        "BEFORE INSERT ON variant_revision "
        "WHEN EXISTS (SELECT 1 FROM variant_revision "
        "WHERE variant_id = NEW.variant_id AND revision = NEW.revision) "
        "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END"
    ),
    "variant_revision_no_delete": (
        "CREATE TRIGGER variant_revision_no_delete "
        "BEFORE DELETE ON variant_revision "
        "WHEN EXISTS (SELECT 1 FROM unit_variant WHERE id = OLD.variant_id) "
        "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END"
    ),
}

# Reference vocabulary of work kinds (open: the admin can add more). Audio is a medium of the
# Edition, not a kind (EF-BIB-001).
WORK_KINDS = (
    # key, label_fr, default_direction, default_media
    ("manga", "Manga", "rtl", "images"),
    ("manhwa", "Manhwa", "ltr", "images"),
    ("manhua", "Manhua", "ltr", "images"),
    ("webtoon", "Webtoon", "vertical", "images"),
    ("comic", "Comic", "ltr", "images"),
    ("bd", "Bande dessinée", "ltr", "images"),
    ("light_novel", "Light novel", None, "text"),
    ("novel", "Roman", None, "text"),
    ("ebook", "Livre numérique", None, "text"),
)

# Relations between distinct Works, never a merge (EF-BIB-039). Open vocabulary.
WORK_RELATION_KINDS = (
    ("adaptation_of", "Adaptation de"),
    ("sequel_of", "Suite de"),
    ("prequel_of", "Préquelle de"),
    ("spin_off_of", "Dérivé de"),
)


def upgrade() -> None:
    op.create_table(
        "age_suggestion",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("value", sa.Integer(), nullable=False),
        sa.Column("x", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("provenance", sa.String(), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.Column("state", sa.String(), server_default="open", nullable=False),
        sa.CheckConstraint(
            "\"entity_type\" IN ('work', 'edition')",
            name=op.f("ck_age_suggestion_entity_type_values"),
        ),
        sa.CheckConstraint(
            "\"provenance\" IN ('comicinfo', 'opf', 'sub_source', 'provider', 'import')",
            name=op.f("ck_age_suggestion_provenance_values"),
        ),
        sa.CheckConstraint(
            "\"state\" IN ('open', 'accepted', 'rejected')",
            name=op.f("ck_age_suggestion_state_values"),
        ),
        sa.CheckConstraint(
            '"value" BETWEEN 0 AND 99', name=op.f("ck_age_suggestion_value_min_age")
        ),
        sa.CheckConstraint(
            'length("entity_id") = 26 AND "entity_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_age_suggestion_entity_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_age_suggestion_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_age_suggestion")),
        sa.UniqueConstraint(
            "entity_type",
            "entity_id",
            "value",
            "x",
            "provenance",
            name=op.f("uq_age_suggestion_entity_type_entity_id_value_x_provenance"),
        ),
    )
    op.create_table(
        "contributor",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(), server_default="person", nullable=False),
        sa.Column("primary_name", sa.String(), nullable=False),
        sa.Column("birth_date", sa.String(), nullable=True),
        sa.Column("birth_precision", sa.String(), nullable=True),
        sa.Column("death_date", sa.String(), nullable=True),
        sa.Column("death_precision", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\"birth_precision\" IN ('year', 'month', 'day')",
            name=op.f("ck_contributor_birth_precision_values"),
        ),
        sa.CheckConstraint(
            "\"death_precision\" IN ('year', 'month', 'day')",
            name=op.f("ck_contributor_death_precision_values"),
        ),
        sa.CheckConstraint(
            "\"kind\" IN ('person', 'org')", name=op.f("ck_contributor_kind_values")
        ),
        sa.CheckConstraint(
            'coalesce(("birth_date" IS NULL AND "birth_precision" IS NULL) OR ("birth_precision" = \'year\' AND length("birth_date") = 4 AND "birth_date" NOT GLOB \'*[^0-9]*\') OR ("birth_precision" = \'month\' AND length("birth_date") = 7 AND date("birth_date" || \'-01\') = "birth_date" || \'-01\') OR ("birth_precision" = \'day\' AND length("birth_date") = 10 AND date("birth_date") = "birth_date"), 0)',
            name=op.f("ck_contributor_birth_precision_coherent"),
        ),
        sa.CheckConstraint(
            'coalesce(("death_date" IS NULL AND "death_precision" IS NULL) OR ("death_precision" = \'year\' AND length("death_date") = 4 AND "death_date" NOT GLOB \'*[^0-9]*\') OR ("death_precision" = \'month\' AND length("death_date") = 7 AND date("death_date" || \'-01\') = "death_date" || \'-01\') OR ("death_precision" = \'day\' AND length("death_date") = 10 AND date("death_date") = "death_date"), 0)',
            name=op.f("ck_contributor_death_precision_coherent"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_contributor_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contributor")),
    )
    op.create_table(
        "external_id",
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("id_scheme", sa.String(), nullable=True),
        sa.Column("state", sa.String(), server_default="current", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\"entity_type\" IN ('series', 'work', 'edition', 'volume', 'contributor')",
            name=op.f("ck_external_id_entity_type_values"),
        ),
        sa.CheckConstraint(
            "\"origin\" IN ('source', 'provider', 'manual', 'legacy')",
            name=op.f("ck_external_id_origin_values"),
        ),
        sa.CheckConstraint(
            "\"provider\" <> '' AND \"provider\" NOT GLOB '*[^a-z0-9_]*'",
            name=op.f("ck_external_id_provider_key"),
        ),
        sa.CheckConstraint(
            "\"state\" IN ('current', 'superseded')", name=op.f("ck_external_id_state_values")
        ),
        sa.CheckConstraint("\"value\" <> ''", name=op.f("ck_external_id_value_not_empty")),
        sa.CheckConstraint(
            'length("entity_id") = 26 AND "entity_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_external_id_entity_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("entity_type", "provider", "value", name=op.f("pk_external_id")),
    )
    op.create_index(
        "ix_external_id_entity", "external_id", ["entity_type", "entity_id"], unique=False
    )
    op.create_table(
        "merge_suggestion",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("a", sa.String(length=26), nullable=False),
        sa.Column("b", sa.String(length=26), nullable=False),
        sa.Column("score", sa.Float(), nullable=True),
        sa.Column("reason", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="open", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint('"a" < "b"', name=op.f("ck_merge_suggestion_pair_ordered")),
        sa.CheckConstraint(
            "\"entity_type\" IN ('work', 'contributor', 'variant')",
            name=op.f("ck_merge_suggestion_entity_type_values"),
        ),
        sa.CheckConstraint("\"reason\" <> ''", name=op.f("ck_merge_suggestion_reason_not_empty")),
        sa.CheckConstraint(
            "\"state\" IN ('open', 'accepted', 'rejected')",
            name=op.f("ck_merge_suggestion_state_values"),
        ),
        sa.CheckConstraint(
            'length("a") = 26 AND "a" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_merge_suggestion_a_ulid"),
        ),
        sa.CheckConstraint(
            'length("b") = 26 AND "b" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_merge_suggestion_b_ulid"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_merge_suggestion_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_merge_suggestion")),
        sa.UniqueConstraint(
            "entity_type",
            "a",
            "b",
            "reason",
            name=op.f("uq_merge_suggestion_entity_type_a_b_reason"),
        ),
    )
    op.create_table(
        "normalizer",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("introduced_in", sa.String(), nullable=False),
        sa.CheckConstraint(
            "\"id\" <> '' AND \"id\" NOT GLOB '*[^a-z0-9_-]*'", name=op.f("ck_normalizer_id_slug")
        ),
        sa.CheckConstraint('"version" >= 1', name=op.f("ck_normalizer_version_min")),
        sa.PrimaryKeyConstraint("id", "version", name=op.f("pk_normalizer")),
    )
    op.create_table(
        "plugin_install",
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("api", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("state_schema", sa.Integer(), nullable=True),
        sa.Column(
            "installed_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\"origin\" IN ('builtin', 'local')", name=op.f("ck_plugin_install_origin_values")
        ),
        sa.CheckConstraint(
            'length("sha256") = 71 AND "sha256" GLOB \'sha256:*\' AND substr("sha256", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_plugin_install_sha256_sha256_prefixed"),
        ),
        sa.PrimaryKeyConstraint("plugin_id", name=op.f("pk_plugin_install")),
    )
    op.create_table(
        "plugin_state",
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("payload_schema", sa.Integer(), server_default="1", nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            '"payload_schema" >= 1', name=op.f("ck_plugin_state_payload_schema_min")
        ),
        sa.PrimaryKeyConstraint("plugin_id", "key", name=op.f("pk_plugin_state")),
    )
    op.create_table(
        "series",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("sort_title", sa.String(), nullable=False),
        sa.Column("reading_order", sa.String(), server_default="publication", nullable=False),
        sa.Column("age_rating", sa.Integer(), nullable=True),
        sa.Column("age_x", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("rated_at", sa.DateTime(), nullable=True),
        sa.Column("issn", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\"reading_order\" IN ('publication', 'chronological', 'custom')",
            name=op.f("ck_series_reading_order_values"),
        ),
        sa.CheckConstraint(
            'age_rating IS NULL OR (typeof("age_rating") = \'integer\' AND "age_rating" BETWEEN 0 AND 99)',
            name=op.f("ck_series_age_rating_min_age"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_series_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_series")),
    )
    op.create_table(
        "source_alias",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("old_source_key", sa.String(), nullable=False),
        sa.Column("old_remote_id", sa.String(), nullable=False),
        sa.Column("new_source_key", sa.String(), nullable=False),
        sa.Column("new_remote_id", sa.String(), nullable=False),
        sa.Column("id_scheme", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_alias")),
        sa.UniqueConstraint(
            "plugin_id",
            "old_source_key",
            "old_remote_id",
            name=op.f("uq_source_alias_plugin_id_old_source_key_old_remote_id"),
        ),
    )
    op.create_table(
        "source_item",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("remote_id", sa.String(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("memo", sa.JSON(), nullable=True),
        sa.Column("memo_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("last_seen", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint('"memo_version" >= 1', name=op.f("ck_source_item_memo_version_min")),
        sa.CheckConstraint(
            "\"plugin_id\" <> '' AND \"source_key\" <> '' AND \"remote_id\" <> ''",
            name=op.f("ck_source_item_key_not_empty"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_source_item_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_item")),
        sa.UniqueConstraint(
            "plugin_id",
            "source_key",
            "remote_id",
            name=op.f("uq_source_item_plugin_id_source_key_remote_id"),
        ),
    )
    op.create_table(
        "sub_source",
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("lang", sa.String(), server_default="und", nullable=False),
        sa.Column("lang_raw", sa.String(), nullable=True),
        sa.Column("content_warning", sa.String(), nullable=True),
        sa.Column("limit_key", sa.String(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            'length("lang") > 0 AND "lang" NOT GLOB \'*[^A-Za-z0-9-]*\' AND "lang" NOT GLOB \'-*\' AND "lang" NOT GLOB \'*-\'',
            name=op.f("ck_sub_source_lang"),
        ),
        sa.PrimaryKeyConstraint("plugin_id", "source_key", name=op.f("pk_sub_source")),
    )
    op.create_table(
        "upstream_snapshot",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("connected_service_id", sa.String(), nullable=False),
        sa.Column("series_ref", sa.String(), nullable=False),
        sa.Column("capability", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("taken_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            '"schema_version" >= 1', name=op.f("ck_upstream_snapshot_schema_version_min")
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_upstream_snapshot_id_ulid"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_upstream_snapshot")),
    )
    op.create_index(
        "ix_upstream_snapshot_series",
        "upstream_snapshot",
        ["connected_service_id", "series_ref", "capability"],
        unique=False,
    )
    op.create_table(
        "work_kind",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("label_fr", sa.String(), nullable=False),
        sa.Column("default_shelf", sa.String(), nullable=True),
        sa.Column("default_direction", sa.String(), nullable=True),
        sa.Column("default_media", sa.String(), server_default="images", nullable=False),
        sa.CheckConstraint(
            "\"default_direction\" IN ('ltr', 'rtl', 'vertical')",
            name=op.f("ck_work_kind_default_direction_values"),
        ),
        sa.CheckConstraint(
            "\"default_media\" IN ('images', 'text', 'file', 'audio')",
            name=op.f("ck_work_kind_default_media_values"),
        ),
        sa.CheckConstraint(
            "\"key\" <> '' AND \"key\" NOT GLOB '*[^a-z0-9_]*'", name=op.f("ck_work_kind_key_slug")
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_work_kind")),
    )
    op.create_table(
        "contribution",
        sa.Column("contributor_id", sa.String(length=26), nullable=False),
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("position", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint(
            "\"entity_type\" IN ('series', 'work', 'edition', 'volume')",
            name=op.f("ck_contribution_entity_type_values"),
        ),
        sa.CheckConstraint('"position" >= 0', name=op.f("ck_contribution_position_min")),
        sa.CheckConstraint(
            'length("entity_id") = 26 AND "entity_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_contribution_entity_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("role") = 3 AND "role" NOT GLOB \'*[^a-z]*\'',
            name=op.f("ck_contribution_role_marc_relator"),
        ),
        sa.ForeignKeyConstraint(
            ["contributor_id"],
            ["contributor.id"],
            name=op.f("fk_contribution_contributor_id_contributor"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "contributor_id", "entity_type", "entity_id", "role", name=op.f("pk_contribution")
        ),
    )
    op.create_index(
        "ix_contribution_entity", "contribution", ["entity_type", "entity_id"], unique=False
    )
    op.create_table(
        "contributor_name",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("contributor_id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("name_raw", sa.String(), nullable=True),
        sa.Column("name_norm_by", sa.String(), nullable=True),
        sa.Column("lang", sa.String(), server_default="und", nullable=False),
        sa.Column("script", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), server_default="display", nullable=False),
        sa.Column("family_first", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint(
            "\"kind\" IN ('display', 'native', 'romanized', 'sort')",
            name=op.f("ck_contributor_name_kind_values"),
        ),
        sa.CheckConstraint(
            '"name_norm_by" IS NULL OR ("name_norm_by" GLOB \'?*@[0-9]*\' AND "name_norm_by" NOT GLOB \'*@*[^0-9]*\' AND "name_norm_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_contributor_name_name_norm_by_versioned"),
        ),
        sa.CheckConstraint(
            'length("lang") > 0 AND "lang" NOT GLOB \'*[^A-Za-z0-9-]*\' AND "lang" NOT GLOB \'-*\' AND "lang" NOT GLOB \'*-\'',
            name=op.f("ck_contributor_name_lang"),
        ),
        sa.ForeignKeyConstraint(
            ["contributor_id"],
            ["contributor.id"],
            name=op.f("fk_contributor_name_contributor_id_contributor"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_contributor_name")),
        sa.UniqueConstraint(
            "contributor_id",
            "kind",
            "lang",
            "name",
            name=op.f("uq_contributor_name_contributor_id_kind_lang_name"),
        ),
    )
    op.create_table(
        "source_account",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), server_default="admin", nullable=False),
        sa.Column("auth_mode", sa.String(), nullable=True),
        sa.Column("auth_kind", sa.String(), nullable=True),
        sa.Column("secret_kid", sa.String(), nullable=True),
        sa.Column("secret_v", sa.Integer(), nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("secret_ct", sa.LargeBinary(), nullable=True),
        sa.Column("session_expires_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\"kind\" IN ('admin', 'service')", name=op.f("ck_source_account_kind_values")
        ),
        sa.CheckConstraint(
            '("secret_kid" IS NULL AND "secret_v" IS NULL AND "secret_nonce" IS NULL AND "secret_ct" IS NULL) OR ("secret_kid" IS NOT NULL AND "secret_v" IS NOT NULL AND "secret_v" >= 1 AND "secret_nonce" IS NOT NULL AND "secret_ct" IS NOT NULL AND length("secret_ct") >= 16)',
            name=op.f("ck_source_account_secret_sealed"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_source_account_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["secret_kid"], ["secret_key.kid"], name=op.f("fk_source_account_secret_kid_secret_key")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_source_account_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_account")),
        sa.UniqueConstraint(
            "user_id", "plugin_id", "kind", name=op.f("uq_source_account_user_id_plugin_id_kind")
        ),
    )
    op.create_table(
        "source_config",
        sa.Column("plugin_id", sa.String(), nullable=False),
        sa.Column("settings", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("secret_kid", sa.String(), nullable=True),
        sa.Column("secret_v", sa.Integer(), nullable=True),
        sa.Column("secret_nonce", sa.LargeBinary(), nullable=True),
        sa.Column("secret_ct", sa.LargeBinary(), nullable=True),
        sa.Column("user_agent", sa.String(), nullable=True),
        sa.Column("rate_limit_override", sa.JSON(), nullable=True),
        sa.Column("terms_url", sa.Text(), nullable=True),
        sa.Column("health", sa.JSON(), nullable=True),
        sa.Column("consecutive_failures", sa.Integer(), server_default="0", nullable=False),
        sa.Column("rights_basis", sa.String(), nullable=True),
        sa.Column("family_safe", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            '"consecutive_failures" >= 0', name=op.f("ck_source_config_consecutive_failures_min")
        ),
        sa.CheckConstraint(
            '("secret_kid" IS NULL AND "secret_v" IS NULL AND "secret_nonce" IS NULL AND "secret_ct" IS NULL) OR ("secret_kid" IS NOT NULL AND "secret_v" IS NOT NULL AND "secret_v" >= 1 AND "secret_nonce" IS NOT NULL AND "secret_ct" IS NOT NULL AND length("secret_ct") >= 16)',
            name=op.f("ck_source_config_secret_sealed"),
        ),
        sa.ForeignKeyConstraint(
            ["secret_kid"], ["secret_key.kid"], name=op.f("fk_source_config_secret_kid_secret_key")
        ),
        sa.PrimaryKeyConstraint("plugin_id", name=op.f("pk_source_config")),
    )
    op.create_table(
        "work",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("sort_title", sa.String(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), server_default="unknown", nullable=False),
        sa.Column("first_published", sa.String(), nullable=True),
        sa.Column("date_precision", sa.String(), nullable=True),
        sa.Column("age_rating", sa.Integer(), nullable=True),
        sa.Column("age_x", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("is_adult", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("rated_at", sa.DateTime(), nullable=True),
        sa.Column("reading_direction", sa.String(), nullable=True),
        sa.Column("locked_fields", sa.JSON(), server_default="[]", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\"date_precision\" IN ('year', 'month', 'day')",
            name=op.f("ck_work_date_precision_values"),
        ),
        sa.CheckConstraint(
            "\"reading_direction\" IN ('ltr', 'rtl', 'vertical')",
            name=op.f("ck_work_reading_direction_values"),
        ),
        sa.CheckConstraint(
            "\"status\" IN ('unknown', 'ongoing', 'completed', 'hiatus', 'cancelled')",
            name=op.f("ck_work_status_values"),
        ),
        sa.CheckConstraint(
            'age_rating IS NULL OR (typeof("age_rating") = \'integer\' AND "age_rating" BETWEEN 0 AND 99)',
            name=op.f("ck_work_age_rating_min_age"),
        ),
        sa.CheckConstraint(
            'coalesce(("first_published" IS NULL AND "date_precision" IS NULL) OR ("date_precision" = \'year\' AND length("first_published") = 4 AND "first_published" NOT GLOB \'*[^0-9]*\') OR ("date_precision" = \'month\' AND length("first_published") = 7 AND date("first_published" || \'-01\') = "first_published" || \'-01\') OR ("date_precision" = \'day\' AND length("first_published") = 10 AND date("first_published") = "first_published"), 0)',
            name=op.f("ck_work_date_precision_coherent"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_work_id_ulid"),
        ),
        sa.ForeignKeyConstraint(["kind"], ["work_kind.key"], name=op.f("fk_work_kind_work_kind")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work")),
    )
    op.create_index("ix_work_kind", "work", ["kind"], unique=False)
    op.create_table(
        "work_relation_kind",
        sa.Column("key", sa.String(), nullable=False),
        sa.Column("label_fr", sa.String(), nullable=False),
        sa.CheckConstraint(
            "\"key\" <> '' AND \"key\" NOT GLOB '*[^a-z0-9_]*'",
            name=op.f("ck_work_relation_kind_key_slug"),
        ),
        sa.PrimaryKeyConstraint("key", name=op.f("pk_work_relation_kind")),
    )
    op.create_table(
        "work_relation",
        sa.Column("from_work_id", sa.String(length=26), nullable=False),
        sa.Column("to_work_id", sa.String(length=26), nullable=False),
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("origin", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint(
            '"from_work_id" <> "to_work_id"', name=op.f("ck_work_relation_distinct_works")
        ),
        sa.CheckConstraint("\"origin\" <> ''", name=op.f("ck_work_relation_origin_not_empty")),
        sa.ForeignKeyConstraint(
            ["from_work_id"],
            ["work.id"],
            name=op.f("fk_work_relation_from_work_id_work"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["kind"],
            ["work_relation_kind.key"],
            name=op.f("fk_work_relation_kind_work_relation_kind"),
        ),
        sa.ForeignKeyConstraint(
            ["to_work_id"],
            ["work.id"],
            name=op.f("fk_work_relation_to_work_id_work"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "from_work_id", "to_work_id", "kind", name=op.f("pk_work_relation")
        ),
    )
    op.create_index("ix_work_relation_to", "work_relation", ["to_work_id"], unique=False)
    op.create_table(
        "edition",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("work_id", sa.String(length=26), nullable=False),
        sa.Column("lang", sa.String(), nullable=False),
        sa.Column("lang_raw", sa.String(), nullable=True),
        sa.Column("translation_type", sa.String(), nullable=False),
        sa.Column("media", sa.String(), nullable=False),
        sa.Column("publisher", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("text_version", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("performance", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("packaging", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("text_version_contributor_id", sa.String(length=26), nullable=True),
        sa.Column("text_version_label", sa.String(), nullable=True),
        sa.Column("text_version_norm_by", sa.String(), nullable=True),
        sa.Column("translated_from_edition_id", sa.String(length=26), nullable=True),
        sa.Column("published_on", sa.String(), nullable=True),
        sa.Column("date_precision", sa.String(), nullable=True),
        sa.Column("is_official", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("preferred", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("age_rating", sa.Integer(), nullable=True),
        sa.Column("age_x", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("rated_at", sa.DateTime(), nullable=True),
        sa.Column("rights", sa.Text(), nullable=True),
        sa.Column("rights_basis", sa.String(), nullable=True),
        sa.Column("rights_jurisdiction", sa.String(), nullable=True),
        sa.Column("author_death_year", sa.Integer(), nullable=True),
        sa.Column("translator", sa.String(), nullable=True),
        sa.Column("translator_death_year", sa.Integer(), nullable=True),
        sa.Column("volume_policy", sa.String(), server_default="closed_only", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\"date_precision\" IN ('year', 'month', 'day')",
            name=op.f("ck_edition_date_precision_values"),
        ),
        sa.CheckConstraint(
            "\"media\" IN ('images', 'text', 'file', 'audio')", name=op.f("ck_edition_media_values")
        ),
        sa.CheckConstraint(
            '("text_version" = \'\' AND "text_version_contributor_id" IS NULL AND "text_version_label" IS NULL) OR ("text_version" <> \'\' AND ("text_version_contributor_id" IS NOT NULL OR "text_version_label" IS NOT NULL))',
            name=op.f("ck_edition_text_version_has_reference"),
        ),
        sa.CheckConstraint(
            '"text_version_norm_by" IS NULL OR ("text_version_norm_by" GLOB \'?*@[0-9]*\' AND "text_version_norm_by" NOT GLOB \'*@*[^0-9]*\' AND "text_version_norm_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_edition_text_version_norm_by_versioned"),
        ),
        sa.CheckConstraint(
            "\"translation_type\" IN ('official', 'fan', 'machine', 'raw')",
            name=op.f("ck_edition_translation_type_values"),
        ),
        sa.CheckConstraint(
            "\"volume_policy\" IN ('closed_only', 'rolling_tail', 'whole')",
            name=op.f("ck_edition_volume_policy_values"),
        ),
        sa.CheckConstraint(
            'age_rating IS NULL OR (typeof("age_rating") = \'integer\' AND "age_rating" BETWEEN 0 AND 99)',
            name=op.f("ck_edition_age_rating_min_age"),
        ),
        sa.CheckConstraint(
            'coalesce(("published_on" IS NULL AND "date_precision" IS NULL) OR ("date_precision" = \'year\' AND length("published_on") = 4 AND "published_on" NOT GLOB \'*[^0-9]*\') OR ("date_precision" = \'month\' AND length("published_on") = 7 AND date("published_on" || \'-01\') = "published_on" || \'-01\') OR ("date_precision" = \'day\' AND length("published_on") = 10 AND date("published_on") = "published_on"), 0)',
            name=op.f("ck_edition_date_precision_coherent"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_edition_id_ulid"),
        ),
        sa.CheckConstraint(
            "length(\"lang\") > 0 AND \"lang\" NOT GLOB '*[^A-Za-z0-9-]*' AND \"lang\" NOT GLOB '-*' AND \"lang\" NOT GLOB '*-' AND lower(\"lang\") NOT IN ('all', 'multi', 'und', 'mul')",
            name=op.f("ck_edition_lang"),
        ),
        sa.CheckConstraint(
            "typeof(\"author_death_year\") IN ('integer', 'null') AND typeof(\"translator_death_year\") IN ('integer', 'null')",
            name=op.f("ck_edition_death_year_integer"),
        ),
        sa.ForeignKeyConstraint(
            ["text_version_contributor_id"],
            ["contributor.id"],
            name=op.f("fk_edition_text_version_contributor_id_contributor"),
        ),
        sa.ForeignKeyConstraint(
            ["translated_from_edition_id"],
            ["edition.id"],
            name=op.f("fk_edition_translated_from_edition_id_edition"),
        ),
        sa.ForeignKeyConstraint(["work_id"], ["work.id"], name=op.f("fk_edition_work_id_work")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_edition")),
        sa.UniqueConstraint("id", "work_id", name="uq_edition_id_work_id"),
        sa.UniqueConstraint(
            "work_id",
            "lang",
            "translation_type",
            "media",
            "publisher",
            "text_version",
            "performance",
            "packaging",
            name="uq_edition_key",
        ),
    )
    op.create_index(
        "ix_edition_text_version_contributor",
        "edition",
        ["text_version_contributor_id"],
        unique=False,
    )
    op.create_index(
        "ix_edition_translated_from", "edition", ["translated_from_edition_id"], unique=False
    )
    op.create_table(
        "series_member",
        sa.Column("series_id", sa.String(length=26), nullable=False),
        sa.Column("work_id", sa.String(length=26), nullable=False),
        sa.Column("position", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=True),
        sa.CheckConstraint(
            "\"position\" GLOB '[0-9]*' AND \"position\" NOT GLOB '*[^0-9.]*' AND \"position\" NOT GLOB '*.*.*' AND \"position\" NOT GLOB '*.'",
            name=op.f("ck_series_member_position_decimal"),
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.id"],
            name=op.f("fk_series_member_series_id_series"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"], ["work.id"], name=op.f("fk_series_member_work_id_work"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("series_id", "work_id", name=op.f("pk_series_member")),
    )
    op.create_index("ix_series_member_work", "series_member", ["work_id"], unique=False)
    op.create_table(
        "work_title",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("work_id", sa.String(length=26), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("lang", sa.String(), server_default="und", nullable=False),
        sa.Column("lang_raw", sa.String(), nullable=True),
        sa.Column("kind", sa.String(), server_default="alt", nullable=False),
        sa.Column("script", sa.String(), nullable=True),
        sa.CheckConstraint(
            "\"kind\" IN ('alt', 'localized', 'romanized')", name=op.f("ck_work_title_kind_values")
        ),
        sa.CheckConstraint(
            'length("lang") > 0 AND "lang" NOT GLOB \'*[^A-Za-z0-9-]*\' AND "lang" NOT GLOB \'-*\' AND "lang" NOT GLOB \'*-\'',
            name=op.f("ck_work_title_lang"),
        ),
        sa.ForeignKeyConstraint(
            ["work_id"], ["work.id"], name=op.f("fk_work_title_work_id_work"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_work_title")),
        sa.UniqueConstraint(
            "work_id", "lang", "kind", "title", name=op.f("uq_work_title_work_id_lang_kind_title")
        ),
    )
    op.create_table(
        "gap_ack",
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("track", sa.String(), nullable=False),
        sa.Column("number_segment", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("number_norm", sa.String(), nullable=False),
        sa.Column("acked_by", sa.String(length=26), nullable=True),
        sa.Column("acked_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "\"track\" IN ('main', 'annual', 'special', 'extra', 'oneshot', 'unnumbered')",
            name=op.f("ck_gap_ack_track_values"),
        ),
        sa.CheckConstraint("\"number_norm\" <> ''", name=op.f("ck_gap_ack_number_norm_not_empty")),
        sa.ForeignKeyConstraint(
            ["acked_by"], ["user.id"], name=op.f("fk_gap_ack_acked_by_user"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["edition.id"],
            name=op.f("fk_gap_ack_edition_id_edition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "edition_id", "track", "number_segment", "number_norm", name=op.f("pk_gap_ack")
        ),
    )
    op.create_table(
        "series_follow",
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("work_id", sa.String(length=26), nullable=False),
        sa.Column("follow_from_segment", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("follow_from_number", sa.String(), nullable=True),
        sa.Column("follow_from_norm_by", sa.String(), nullable=True),
        sa.Column("backfill_mode", sa.String(), server_default="none", nullable=False),
        sa.Column("cap", sa.Integer(), nullable=True),
        sa.Column("upgrade_policy", sa.String(), server_default="keep_first", nullable=False),
        sa.Column("check_override", sa.String(), nullable=True),
        sa.Column("filters", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "\"backfill_mode\" IN ('none', 'gaps', 'all')",
            name=op.f("ck_series_follow_backfill_mode_values"),
        ),
        sa.CheckConstraint('"cap" >= 1', name=op.f("ck_series_follow_cap_min")),
        sa.CheckConstraint(
            '"filters" IS NULL OR CASE WHEN json_valid("filters") THEN json_type("filters") = \'object\' ELSE 0 END',
            name=op.f("ck_series_follow_filters_json_object"),
        ),
        sa.CheckConstraint(
            "\"check_override\" IN ('completed', 'paused')",
            name=op.f("ck_series_follow_check_override_values"),
        ),
        sa.CheckConstraint(
            '"follow_from_norm_by" IS NULL OR ("follow_from_norm_by" GLOB \'?*@[0-9]*\' AND "follow_from_norm_by" NOT GLOB \'*@*[^0-9]*\' AND "follow_from_norm_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_series_follow_follow_from_norm_by_versioned"),
        ),
        sa.CheckConstraint(
            "\"upgrade_policy\" IN ('keep_first', 'prefer_rank')",
            name=op.f("ck_series_follow_upgrade_policy_values"),
        ),
        sa.ForeignKeyConstraint(
            ["edition_id", "work_id"],
            ["edition.id", "edition.work_id"],
            name=op.f("fk_series_follow_edition_id_work_id_edition"),
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["edition.id"],
            name=op.f("fk_series_follow_edition_id_edition"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["work_id"], ["work.id"], name=op.f("fk_series_follow_work_id_work")
        ),
        sa.PrimaryKeyConstraint("edition_id", name=op.f("pk_series_follow")),
    )
    op.create_table(
        "source_binding",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("work_id", sa.String(length=26), nullable=False),
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("source_item_id", sa.String(length=26), nullable=False),
        sa.Column("priority", sa.Integer(), server_default="1", nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("disabled_reason", sa.String(), nullable=True),
        sa.Column("check_cron", sa.String(), nullable=True),
        sa.Column("next_expected", sa.DateTime(), nullable=True),
        sa.Column("last_checked", sa.DateTime(), nullable=True),
        sa.Column("content_trust", sa.String(), server_default="untrusted", nullable=False),
        sa.Column(
            "linked_from_adoption", sa.Boolean(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\"content_trust\" IN ('publisher', 'untrusted')",
            name=op.f("ck_source_binding_content_trust_values"),
        ),
        sa.CheckConstraint(
            "\"disabled_reason\" IN ('not_loaded', 'content_failures', 'removed_upstream', 'manual')",
            name=op.f("ck_source_binding_disabled_reason_values"),
        ),
        sa.CheckConstraint('"priority" >= 1', name=op.f("ck_source_binding_priority_min")),
        sa.CheckConstraint(
            '("active" AND "disabled_reason" IS NULL) OR (NOT "active" AND "disabled_reason" IS NOT NULL)',
            name=op.f("ck_source_binding_disabled_reason_matches_active"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_source_binding_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["edition_id", "work_id"],
            ["edition.id", "edition.work_id"],
            name=op.f("fk_source_binding_edition_id_work_id_edition"),
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["edition.id"], name=op.f("fk_source_binding_edition_id_edition")
        ),
        sa.ForeignKeyConstraint(
            ["source_item_id"],
            ["source_item.id"],
            name=op.f("fk_source_binding_source_item_id_source_item"),
        ),
        sa.ForeignKeyConstraint(
            ["work_id"], ["work.id"], name=op.f("fk_source_binding_work_id_work")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_source_binding")),
        sa.UniqueConstraint(
            "edition_id", "source_item_id", name=op.f("uq_source_binding_edition_id_source_item_id")
        ),
    )
    op.create_index(
        "ix_source_binding_source_item", "source_binding", ["source_item_id"], unique=False
    )
    op.create_index("ix_source_binding_work", "source_binding", ["work_id"], unique=False)
    op.create_index(
        "uq_source_binding_active_priority",
        "source_binding",
        ["edition_id", "priority"],
        unique=True,
        sqlite_where=sa.text('"active"'),
    )
    op.create_table(
        "team",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("label_raw", sa.String(), nullable=True),
        sa.Column("label_norm_by", sa.String(), nullable=True),
        sa.Column("last_release_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            '"label_norm_by" IS NULL OR ("label_norm_by" GLOB \'?*@[0-9]*\' AND "label_norm_by" NOT GLOB \'*@*[^0-9]*\' AND "label_norm_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_team_label_norm_by_versioned"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_team_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["edition.id"], name=op.f("fk_team_edition_id_edition")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_team")),
        sa.UniqueConstraint("edition_id", "label", name=op.f("uq_team_edition_id_label")),
        sa.UniqueConstraint("id", "edition_id", name=op.f("uq_team_id_edition_id")),
    )
    op.create_table(
        "volume",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("number", sa.String(), nullable=False),
        sa.Column("number_sort", sa.Float(), nullable=True),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("isbn", sa.String(), nullable=True),
        sa.Column("cover_ref", sa.String(), nullable=True),
        sa.Column("closed", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            '"isbn" IS NULL OR (length("isbn") = 13 AND "isbn" NOT GLOB \'*[^0-9]*\')',
            name=op.f("ck_volume_isbn_13_digits"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_volume_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["edition.id"], name=op.f("fk_volume_edition_id_edition")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_volume")),
        sa.UniqueConstraint("edition_id", "number", name=op.f("uq_volume_edition_id_number")),
    )
    op.create_table(
        "source_failure",
        sa.Column("source_binding_id", sa.String(length=26), nullable=False),
        sa.Column("limit_key", sa.String(), nullable=False),
        sa.Column("failure_class", sa.String(), nullable=False),
        sa.Column("consecutive_content_errors", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error_code", sa.String(), nullable=True),
        sa.Column("last_error_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            '"consecutive_content_errors" >= 0',
            name=op.f("ck_source_failure_consecutive_content_errors_min"),
        ),
        sa.CheckConstraint(
            "\"failure_class\" IN ('content', 'auth', 'version', 'schema', 'infra')",
            name=op.f("ck_source_failure_failure_class_values"),
        ),
        sa.ForeignKeyConstraint(
            ["source_binding_id"],
            ["source_binding.id"],
            name=op.f("fk_source_failure_source_binding_id_source_binding"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "source_binding_id", "limit_key", "failure_class", name=op.f("pk_source_failure")
        ),
    )
    op.create_table(
        "team_alias",
        sa.Column("team_id", sa.String(length=26), nullable=False),
        sa.Column("source_binding_id", sa.String(length=26), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_binding_id"],
            ["source_binding.id"],
            name=op.f("fk_team_alias_source_binding_id_source_binding"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.id"], name=op.f("fk_team_alias_team_id_team"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint(
            "team_id", "source_binding_id", "label", name=op.f("pk_team_alias")
        ),
    )
    op.create_index("ix_team_alias_binding", "team_alias", ["source_binding_id"], unique=False)
    op.create_table(
        "team_priority",
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("team_id", sa.String(length=26), nullable=False),
        sa.Column("rank", sa.Integer(), nullable=False),
        sa.CheckConstraint('"rank" >= 1', name=op.f("ck_team_priority_rank_min")),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["edition.id"],
            name=op.f("fk_team_priority_edition_id_edition"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["team_id", "edition_id"],
            ["team.id", "team.edition_id"],
            name=op.f("fk_team_priority_team_id_edition_id_team"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("edition_id", "team_id", name=op.f("pk_team_priority")),
        sa.UniqueConstraint("edition_id", "rank", name=op.f("uq_team_priority_edition_id_rank")),
    )
    op.create_table(
        "unit",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("volume_id", sa.String(length=26), nullable=True),
        sa.Column("number_raw", sa.String(), nullable=False),
        sa.Column("number_norm", sa.String(), nullable=False),
        sa.Column("number_norm_by", sa.String(), nullable=True),
        sa.Column("number_sort", sa.Float(), nullable=True),
        sa.Column("number_segment", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("number_segment_norm_by", sa.String(), nullable=True),
        sa.Column("number_segment_ordinal", sa.Integer(), server_default="0", nullable=False),
        sa.Column("part", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("track", sa.String(), server_default="main", nullable=False),
        sa.Column("source_order", sa.Integer(), server_default="0", nullable=False),
        sa.Column("kind", sa.String(), server_default="chapter", nullable=False),
        sa.Column("title", sa.String(), nullable=True),
        sa.Column("published_on", sa.String(), nullable=True),
        sa.Column("date_precision", sa.String(), nullable=True),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        sa.Column("availability", sa.String(), server_default="free", nullable=False),
        sa.Column("available_until", sa.DateTime(), nullable=True),
        sa.Column("state", sa.String(), server_default="known", nullable=False),
        sa.Column("follow_suppressed", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column("follow_suppressed_by", sa.String(length=26), nullable=True),
        sa.Column("follow_suppressed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "\"availability\" IN ('free', 'purchased', 'expiring', 'locked', 'stubbed', 'removed')",
            name=op.f("ck_unit_availability_values"),
        ),
        sa.CheckConstraint(
            "\"date_precision\" IN ('year', 'month', 'day')",
            name=op.f("ck_unit_date_precision_values"),
        ),
        sa.CheckConstraint(
            "\"kind\" IN ('chapter', 'episode', 'book')", name=op.f("ck_unit_kind_values")
        ),
        sa.CheckConstraint(
            '"number_norm_by" IS NULL OR ("number_norm_by" GLOB \'?*@[0-9]*\' AND "number_norm_by" NOT GLOB \'*@*[^0-9]*\' AND "number_norm_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_unit_number_norm_by_versioned"),
        ),
        sa.CheckConstraint(
            '"number_segment_norm_by" IS NULL OR ("number_segment_norm_by" GLOB \'?*@[0-9]*\' AND "number_segment_norm_by" NOT GLOB \'*@*[^0-9]*\' AND "number_segment_norm_by" NOT GLOB \'*@*@*\')',
            name=op.f("ck_unit_number_segment_norm_by_versioned"),
        ),
        sa.CheckConstraint(
            '"number_segment_ordinal" >= 0', name=op.f("ck_unit_number_segment_ordinal_min")
        ),
        sa.CheckConstraint('"source_order" >= 0', name=op.f("ck_unit_source_order_min")),
        sa.CheckConstraint(
            "\"state\" IN ('known', 'wanted', 'queued', 'stored', 'exported', 'adopted', 'missing', 'store_missing', 'corrupt', 'source_removed', 'quarantined')",
            name=op.f("ck_unit_state_values"),
        ),
        sa.CheckConstraint(
            "\"track\" = 'unnumbered' OR \"number_norm\" <> ''",
            name=op.f("ck_unit_numbered_track_has_number"),
        ),
        sa.CheckConstraint(
            "\"track\" IN ('main', 'annual', 'special', 'extra', 'oneshot', 'unnumbered')",
            name=op.f("ck_unit_track_values"),
        ),
        sa.CheckConstraint(
            'coalesce(("published_on" IS NULL AND "date_precision" IS NULL) OR ("date_precision" = \'year\' AND length("published_on") = 4 AND "published_on" NOT GLOB \'*[^0-9]*\') OR ("date_precision" = \'month\' AND length("published_on") = 7 AND date("published_on" || \'-01\') = "published_on" || \'-01\') OR ("date_precision" = \'day\' AND length("published_on") = 10 AND date("published_on") = "published_on"), 0)',
            name=op.f("ck_unit_date_precision_coherent"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_unit_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"], ["edition.id"], name=op.f("fk_unit_edition_id_edition")
        ),
        sa.ForeignKeyConstraint(
            ["follow_suppressed_by"],
            ["user.id"],
            name=op.f("fk_unit_follow_suppressed_by_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["volume_id"], ["volume.id"], name=op.f("fk_unit_volume_id_volume"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unit")),
    )
    op.create_index("ix_unit_state", "unit", ["edition_id", "state"], unique=False)
    op.create_index("ix_unit_volume", "unit", ["volume_id"], unique=False)
    op.create_index(
        "uq_unit_place",
        "unit",
        ["edition_id", "track", "number_segment", "number_norm", "part"],
        unique=True,
        sqlite_where=sa.text("\"track\" <> 'unnumbered'"),
    )
    op.create_index(
        "uq_unit_unnumbered_order",
        "unit",
        ["edition_id", "source_order"],
        unique=True,
        sqlite_where=sa.text("\"track\" = 'unnumbered'"),
    )
    op.create_table(
        "unit_variant",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("unit_id", sa.String(length=26), nullable=False),
        sa.Column("source_binding_id", sa.String(length=26), nullable=False),
        sa.Column("remote_unit_id", sa.String(), nullable=False),
        sa.Column("source_item_id", sa.String(length=26), nullable=True),
        sa.Column("team_id", sa.String(length=26), nullable=True),
        sa.Column("lang", sa.String(), server_default="und", nullable=False),
        sa.Column("lang_raw", sa.String(), nullable=True),
        sa.Column("revision", sa.Integer(), server_default="1", nullable=False),
        sa.Column("revision_hint", sa.String(), nullable=True),
        sa.Column("supersedes_variant_id", sa.String(length=26), nullable=True),
        sa.Column("fetch_handle", sa.String(), nullable=True),
        sa.Column("fetch_handle_seen_at", sa.DateTime(), nullable=True),
        sa.Column("quality_score", sa.Float(), nullable=True),
        sa.Column("preferred", sa.Boolean(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("removed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "\"remote_unit_id\" <> ''", name=op.f("ck_unit_variant_remote_unit_id_not_empty")
        ),
        sa.CheckConstraint('"revision" >= 1', name=op.f("ck_unit_variant_revision_min")),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_unit_variant_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("lang") > 0 AND "lang" NOT GLOB \'*[^A-Za-z0-9-]*\' AND "lang" NOT GLOB \'-*\' AND "lang" NOT GLOB \'*-\'',
            name=op.f("ck_unit_variant_lang"),
        ),
        sa.ForeignKeyConstraint(
            ["source_binding_id"],
            ["source_binding.id"],
            name=op.f("fk_unit_variant_source_binding_id_source_binding"),
        ),
        sa.ForeignKeyConstraint(
            ["source_item_id"],
            ["source_item.id"],
            name=op.f("fk_unit_variant_source_item_id_source_item"),
        ),
        sa.ForeignKeyConstraint(
            ["supersedes_variant_id"],
            ["unit_variant.id"],
            name=op.f("fk_unit_variant_supersedes_variant_id_unit_variant"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["team_id"], ["team.id"], name=op.f("fk_unit_variant_team_id_team"), ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"], ["unit.id"], name=op.f("fk_unit_variant_unit_id_unit")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_unit_variant")),
        sa.UniqueConstraint(
            "source_binding_id",
            "remote_unit_id",
            name=op.f("uq_unit_variant_source_binding_id_remote_unit_id"),
        ),
    )
    op.create_index(
        "ix_unit_variant_supersedes", "unit_variant", ["supersedes_variant_id"], unique=False
    )
    op.create_index("ix_unit_variant_team", "unit_variant", ["team_id"], unique=False)
    op.create_index("ix_unit_variant_unit", "unit_variant", ["unit_id"], unique=False)
    op.create_index(
        "uq_unit_variant_preferred",
        "unit_variant",
        ["unit_id"],
        unique=True,
        sqlite_where=sa.text('"preferred"'),
    )
    op.create_table(
        "download_request",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("target_kind", sa.String(), nullable=False),
        sa.Column("variant_id", sa.String(length=26), nullable=True),
        sa.Column("range_ref", sa.JSON(), nullable=True),
        sa.Column("source_binding_id", sa.String(length=26), nullable=False),
        sa.Column("ticket", sa.String(), nullable=True),
        sa.Column("state", sa.String(), server_default="requested", nullable=False),
        sa.Column(
            "requested_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.CheckConstraint('"attempts" >= 0', name=op.f("ck_download_request_attempts_min")),
        sa.CheckConstraint(
            "\"state\" IN ('requested', 'downloaded', 'fetched', 'cancelled', 'abandoned', 'closed_by_purge')",
            name=op.f("ck_download_request_state_values"),
        ),
        sa.CheckConstraint(
            "\"target_kind\" IN ('unit', 'range')",
            name=op.f("ck_download_request_target_kind_values"),
        ),
        sa.CheckConstraint(
            '("target_kind" = \'unit\' AND ("variant_id" IS NOT NULL OR "state" = \'closed_by_purge\') AND "range_ref" IS NULL) OR ("target_kind" = \'range\' AND "variant_id" IS NULL AND "range_ref" IS NOT NULL)',
            name=op.f("ck_download_request_target_matches_kind"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_download_request_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["source_binding_id"],
            ["source_binding.id"],
            name=op.f("fk_download_request_source_binding_id_source_binding"),
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["unit_variant.id"],
            name=op.f("fk_download_request_variant_id_unit_variant"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_download_request")),
    )
    op.create_index("ix_download_request_state", "download_request", ["state"], unique=False)
    op.create_index("ix_download_request_variant", "download_request", ["variant_id"], unique=False)
    op.create_index(
        "uq_download_request_ticket",
        "download_request",
        ["source_binding_id", "ticket"],
        unique=True,
        sqlite_where=sa.text('"ticket" IS NOT NULL'),
    )
    op.create_table(
        "revision_pending",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("variant_id", sa.String(length=26), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("nature", sa.String(), nullable=False),
        sa.Column("koreader_impact", sa.String(), nullable=True),
        sa.Column("state", sa.String(), server_default="open", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "\"nature\" IN ('metadata_only', 'rewrite')",
            name=op.f("ck_revision_pending_nature_values"),
        ),
        sa.CheckConstraint('"revision" >= 1', name=op.f("ck_revision_pending_revision_min")),
        sa.CheckConstraint(
            "\"state\" IN ('open', 'applied', 'dismissed')",
            name=op.f("ck_revision_pending_state_values"),
        ),
        sa.CheckConstraint(
            'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_revision_pending_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["unit_variant.id"],
            name=op.f("fk_revision_pending_variant_id_unit_variant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_revision_pending")),
        sa.UniqueConstraint(
            "variant_id", "revision", name=op.f("uq_revision_pending_variant_id_revision")
        ),
    )
    op.create_table(
        "variant_revision",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("variant_id", sa.String(length=26), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("seen_at", sa.DateTime(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("content_hash", sa.String(), nullable=True),
        sa.Column("revision_hint", sa.String(), nullable=True),
        sa.CheckConstraint('"page_count" >= 0', name=op.f("ck_variant_revision_page_count_min")),
        sa.CheckConstraint('"revision" >= 1', name=op.f("ck_variant_revision_revision_min")),
        sa.CheckConstraint(
            'length("content_hash") = 71 AND "content_hash" GLOB \'sha256:*\' AND substr("content_hash", 8) NOT GLOB \'*[^0-9a-f]*\'',
            name=op.f("ck_variant_revision_content_hash_sha256_prefixed"),
        ),
        sa.ForeignKeyConstraint(
            ["variant_id"],
            ["unit_variant.id"],
            name=op.f("fk_variant_revision_variant_id_unit_variant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_variant_revision")),
        sa.UniqueConstraint(
            "variant_id", "revision", name=op.f("uq_variant_revision_variant_id_revision")
        ),
    )
    op.create_table(
        "download_request_unit",
        sa.Column("download_request_id", sa.String(length=26), nullable=False),
        sa.Column("unit_id", sa.String(length=26), nullable=False),
        sa.Column("state", sa.String(), server_default="requested", nullable=False),
        sa.CheckConstraint(
            "\"state\" IN ('requested', 'covered', 'missing')",
            name=op.f("ck_download_request_unit_state_values"),
        ),
        sa.ForeignKeyConstraint(
            ["download_request_id"],
            ["download_request.id"],
            name=op.f("fk_download_request_unit_download_request_id_download_request"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["unit_id"],
            ["unit.id"],
            name=op.f("fk_download_request_unit_unit_id_unit"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "download_request_id", "unit_id", name=op.f("pk_download_request_unit")
        ),
    )
    op.create_index(
        "ix_download_request_unit_unit", "download_request_unit", ["unit_id"], unique=False
    )

    work_kind = sa.table(
        "work_kind",
        sa.column("key", sa.String),
        sa.column("label_fr", sa.String),
        sa.column("default_direction", sa.String),
        sa.column("default_media", sa.String),
    )
    op.bulk_insert(
        work_kind,
        [
            {"key": key, "label_fr": label, "default_direction": direction, "default_media": media}
            for key, label, direction, media in WORK_KINDS
        ],
    )

    relation_kind = sa.table(
        "work_relation_kind", sa.column("key", sa.String), sa.column("label_fr", sa.String)
    )
    op.bulk_insert(
        relation_kind,
        [{"key": key, "label_fr": label} for key, label in WORK_RELATION_KINDS],
    )

    for statement in TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    for table in TABLES:  # children first; indexes go with their table
        op.drop_table(table)
