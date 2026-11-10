"""M2 schema (PLAN-M2 L01): what the sources of M2 need and 0001 to 0017 do not hold.

New tables: `metadata_field`, `metadata_cache`, `provider_quota`, `http_cache`, `import_entry`,
`plugin_attestation`, `series_gap`, `binding_release_sample`. New columns:
`content.watermark_origin`, `edition.volume_size|upstream_status|upstream_status_by|
upstream_status_at` (D5), `instance.jurisdiction_confirmed_at` (D4), `unit.availability_detail`
(D20), `unit.filtered_reason`, `variant_revision.quarantined_at|quarantine_reason`,
`shelf.skip_read_upgrades|link_mode`.

No existing row is rewritten: every new column has a default or is nullable. The append-only guard
of `variant_revision` is replaced by one that lets the quarantine mark through, once.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from alembic import op
from alembic.operations.base import BatchOperations

revision = "0020"
down_revision = "0017"
branch_labels = None
depends_on = None

ULID = 'length("entity_id") = 26 AND "entity_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\''
LANG = (
    'length("lang") > 0 AND "lang" NOT GLOB \'*[^A-Za-z0-9-]*\' '
    "AND \"lang\" NOT GLOB '-*' AND \"lang\" NOT GLOB '*-'"
)


def _relative(column: str) -> str:
    c = f'"{column}"'
    return (
        f"{c} NOT GLOB '/*' AND {c} <> '..' AND {c} NOT GLOB '../*' "
        f"AND {c} NOT GLOB '*/../*' AND {c} NOT GLOB '*/..'"
    )


def _sha256(column: str) -> str:
    c = f'"{column}"'
    return f"length({c}) = 71 AND {c} GLOB 'sha256:*' AND substr({c}, 8) NOT GLOB '*[^0-9a-f]*'"


NO_UPDATE_BEFORE = (
    "CREATE TRIGGER variant_revision_no_update "
    "BEFORE UPDATE ON variant_revision "
    "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END"
)
NO_UPDATE_AFTER = (
    "CREATE TRIGGER variant_revision_no_update "
    "BEFORE UPDATE ON variant_revision "
    "WHEN OLD.id IS NOT NEW.id OR OLD.variant_id IS NOT NEW.variant_id "
    "OR OLD.revision IS NOT NEW.revision OR OLD.seen_at IS NOT NEW.seen_at "
    "OR OLD.page_count IS NOT NEW.page_count OR OLD.content_hash IS NOT NEW.content_hash "
    "OR OLD.revision_hint IS NOT NEW.revision_hint OR OLD.quarantined_at IS NOT NULL "
    "BEGIN SELECT RAISE(ABORT, 'variant_revision is append-only'); END"
)
CONTENT_COVERAGE_CLEANUP = (
    "CREATE TRIGGER content_coverage_cleanup "
    "AFTER DELETE ON content "
    "BEGIN DELETE FROM unit_coverage "
    "WHERE carrier_type = 'content' AND carrier_id = OLD.id; END"
)


def _now(name: str) -> sa.Column[datetime]:
    return sa.Column(
        name, sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )


def _blank(name: str) -> sa.Column[str]:
    return sa.Column(name, sa.String(), server_default=sa.text("''"), nullable=False)


@contextmanager
def _rebuild(table: str) -> Iterator[BatchOperations]:
    """Rebuild in batch mode (a CHECK cannot be added in place), restating the defaults that
    reflection would render differently. The triggers of the table go with it: they come back."""
    restated: dict[str, list[sa.Column[Any]]] = {
        "edition": [
            _now("created_at"),
            *(_blank(n) for n in ("publisher", "text_version", "performance", "packaging")),
        ],
        "content": [_now("created_at")],
        "shelf": [_now("created_at")],
    }
    if table == "content":
        op.execute("DROP TRIGGER content_coverage_cleanup")
    with op.batch_alter_table(
        table, recreate="always", reflect_args=tuple(restated[table])
    ) as batch:
        yield batch
    if table == "content":
        op.execute(CONTENT_COVERAGE_CLEANUP)


def upgrade() -> None:
    op.create_table(
        "metadata_field",
        sa.Column("entity_type", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(length=26), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("lang", sa.String(), server_default="und", nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("provenance", sa.String(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=True),
        sa.Column("locked", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(ULID, name=op.f("ck_metadata_field_entity_id_ulid")),
        sa.CheckConstraint(
            "\"entity_type\" IN ('series', 'work', 'edition', 'volume', 'contributor')",
            name=op.f("ck_metadata_field_entity_type_values"),
        ),
        sa.CheckConstraint(
            "\"provenance\" IN ('provider', 'source', 'embedded', 'import', 'manual')",
            name=op.f("ck_metadata_field_provenance_values"),
        ),
        sa.CheckConstraint(LANG, name=op.f("ck_metadata_field_lang")),
        sa.CheckConstraint(
            "\"field\" <> '' AND \"provider\" <> ''",
            name=op.f("ck_metadata_field_field_provider_named"),
        ),
        sa.CheckConstraint(
            '"confidence" IS NULL OR "confidence" BETWEEN 0 AND 1',
            name=op.f("ck_metadata_field_confidence_range"),
        ),
        sa.PrimaryKeyConstraint(
            "entity_type", "entity_id", "field", "lang", name=op.f("pk_metadata_field")
        ),
    )
    op.create_index(
        "ix_metadata_field_entity", "metadata_field", ["entity_type", "entity_id"], unique=False
    )

    op.create_table(
        "metadata_cache",
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint('json_valid("payload")', name=op.f("ck_metadata_cache_payload_json")),
        sa.PrimaryKeyConstraint("provider", "key_hash", name=op.f("pk_metadata_cache")),
    )
    op.create_index("ix_metadata_cache_expires", "metadata_cache", ["expires_at"], unique=False)

    op.create_table(
        "provider_quota",
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("tokens", sa.Float(), nullable=False),
        sa.Column("refilled_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint('"tokens" >= 0', name=op.f("ck_provider_quota_tokens_min")),
        sa.PrimaryKeyConstraint("provider", name=op.f("pk_provider_quota")),
    )

    op.create_table(
        "http_cache",
        sa.Column("scope", sa.String(), nullable=False),
        sa.Column("url_hash", sa.String(), nullable=False),
        sa.Column("etag", sa.String(), nullable=True),
        sa.Column("last_modified", sa.String(), nullable=True),
        sa.Column("stored_at", sa.DateTime(), nullable=False),
        sa.Column("max_age", sa.Integer(), nullable=False),
        sa.Column("body_ref", sa.Text(), nullable=True),
        sa.CheckConstraint('"max_age" >= 0', name=op.f("ck_http_cache_max_age_min")),
        sa.CheckConstraint(_relative("body_ref"), name=op.f("ck_http_cache_body_ref_relative")),
        sa.PrimaryKeyConstraint("scope", "url_hash", name=op.f("pk_http_cache")),
    )

    op.create_table(
        "import_entry",
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("mtime", sa.DateTime(), nullable=False),
        sa.Column("sha256", sa.String(), nullable=True),
        sa.Column("state", sa.String(), server_default="seen", nullable=False),
        sa.Column("reason", sa.String(), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(_relative("rel_path"), name=op.f("ck_import_entry_rel_path_relative")),
        sa.CheckConstraint(_sha256("sha256"), name=op.f("ck_import_entry_sha256_sha256_prefixed")),
        sa.CheckConstraint(
            "\"state\" IN ('seen', 'stored', 'refused', 'removed')",
            name=op.f("ck_import_entry_state_values"),
        ),
        sa.CheckConstraint('"size" >= 0', name=op.f("ck_import_entry_size_min")),
        sa.PrimaryKeyConstraint("rel_path", name=op.f("pk_import_entry")),
    )
    op.create_index("ix_import_entry_state", "import_entry", ["state"], unique=False)

    op.create_table(
        "plugin_attestation",
        sa.Column("plugin_key", sa.String(), nullable=False),
        sa.Column("field", sa.String(), nullable=False),
        sa.Column("value", sa.String(), nullable=False),
        sa.Column("attested_by", sa.String(length=26), nullable=True),
        sa.Column("attested_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "\"field\" IN ('content_trust', 'publisher', 'access_partitioning', 'egress')",
            name=op.f("ck_plugin_attestation_field_values"),
        ),
        sa.CheckConstraint(
            "\"plugin_key\" <> ''", name=op.f("ck_plugin_attestation_plugin_key_not_empty")
        ),
        sa.ForeignKeyConstraint(
            ["attested_by"],
            ["user.id"],
            name=op.f("fk_plugin_attestation_attested_by_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("plugin_key", "field", name=op.f("pk_plugin_attestation")),
    )

    op.create_table(
        "series_gap",
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("track", sa.String(), nullable=False),
        sa.Column("number_segment", sa.String(), server_default=sa.text("''"), nullable=False),
        sa.Column("number_norm", sa.String(), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("notified_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "\"track\" IN ('main', 'annual', 'special', 'extra', 'oneshot', 'unnumbered')",
            name=op.f("ck_series_gap_track_values"),
        ),
        sa.CheckConstraint(
            "\"number_norm\" <> ''", name=op.f("ck_series_gap_number_norm_not_empty")
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["edition.id"],
            name=op.f("fk_series_gap_edition_id_edition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "edition_id", "track", "number_segment", "number_norm", name=op.f("pk_series_gap")
        ),
    )

    op.create_table(
        "binding_release_sample",
        sa.Column("source_binding_id", sa.String(length=26), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["source_binding_id"],
            ["source_binding.id"],
            name=op.f("fk_binding_release_sample_source_binding_id_source_binding"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "source_binding_id", "observed_at", name=op.f("pk_binding_release_sample")
        ),
    )

    op.add_column("instance", sa.Column("jurisdiction_confirmed_at", sa.DateTime(), nullable=True))
    op.add_column("unit", sa.Column("availability_detail", sa.String(), nullable=True))
    op.add_column("unit", sa.Column("filtered_reason", sa.String(), nullable=True))
    op.add_column("variant_revision", sa.Column("quarantined_at", sa.DateTime(), nullable=True))
    op.add_column("variant_revision", sa.Column("quarantine_reason", sa.String(), nullable=True))
    op.execute("DROP TRIGGER variant_revision_no_update")
    op.execute(NO_UPDATE_AFTER)

    with _rebuild("edition") as batch:
        batch.add_column(
            sa.Column("volume_size", sa.Integer(), server_default="50", nullable=False)
        )
        batch.add_column(
            sa.Column("upstream_status", sa.String(), server_default="unknown", nullable=False)
        )
        batch.add_column(sa.Column("upstream_status_by", sa.String(), nullable=True))
        batch.add_column(sa.Column("upstream_status_at", sa.DateTime(), nullable=True))
        batch.create_check_constraint(op.f("ck_edition_volume_size_min"), '"volume_size" >= 1')
        batch.create_check_constraint(
            op.f("ck_edition_upstream_status_values"),
            "\"upstream_status\" IN ('ongoing', 'finished', 'paused', 'unknown')",
        )
        batch.create_check_constraint(
            op.f("ck_edition_upstream_status_by_values"),
            "\"upstream_status_by\" IN ('provider', 'source', 'embedded', 'import', 'manual')",
        )
    with _rebuild("content") as batch:
        batch.add_column(
            sa.Column("watermark_origin", sa.String(), server_default="none", nullable=False)
        )
        batch.create_check_constraint(
            op.f("ck_content_watermark_origin_values"),
            "\"watermark_origin\" IN ('none', 'heuristic', 'user')",
        )
    with _rebuild("shelf") as batch:
        batch.add_column(
            sa.Column("skip_read_upgrades", sa.Boolean(), server_default=sa.true(), nullable=False)
        )
        batch.add_column(sa.Column("link_mode", sa.String(), server_default="copy", nullable=False))
        batch.create_check_constraint(
            op.f("ck_shelf_link_mode_values"), "\"link_mode\" IN ('copy', 'hardlink')"
        )


def downgrade() -> None:
    with _rebuild("shelf") as batch:
        batch.drop_constraint(op.f("ck_shelf_link_mode_values"))
        batch.drop_column("link_mode")
        batch.drop_column("skip_read_upgrades")
    with _rebuild("content") as batch:
        batch.drop_constraint(op.f("ck_content_watermark_origin_values"))
        batch.drop_column("watermark_origin")
    with _rebuild("edition") as batch:
        batch.drop_constraint(op.f("ck_edition_upstream_status_by_values"))
        batch.drop_constraint(op.f("ck_edition_upstream_status_values"))
        batch.drop_constraint(op.f("ck_edition_volume_size_min"))
        batch.drop_column("upstream_status_at")
        batch.drop_column("upstream_status_by")
        batch.drop_column("upstream_status")
        batch.drop_column("volume_size")

    op.execute("DROP TRIGGER variant_revision_no_update")
    op.execute(NO_UPDATE_BEFORE)
    # native `ALTER TABLE ... DROP COLUMN`: a batch rebuild would lose the partial indexes of `unit`
    op.drop_column("variant_revision", "quarantine_reason")
    op.drop_column("variant_revision", "quarantined_at")
    op.drop_column("unit", "filtered_reason")
    op.drop_column("unit", "availability_detail")
    op.drop_column("instance", "jurisdiction_confirmed_at")

    op.drop_table("binding_release_sample")
    op.drop_table("series_gap")
    op.drop_table("plugin_attestation")
    op.drop_table("import_entry")
    op.drop_table("http_cache")
    op.drop_table("provider_quota")
    op.drop_table("metadata_cache")
    op.drop_table("metadata_field")
