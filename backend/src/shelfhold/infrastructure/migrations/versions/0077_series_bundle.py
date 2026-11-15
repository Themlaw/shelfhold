"""The block of a series (M4 L49a, ADR-0053 §3, §5, §8, ADR-0048).

Three tables (`bundle_partition`, `bundle_version`, `bundle_member`) and one trigger. The DDL is the
one SQLAlchemy writes for `infrastructure/schema/bundle.py`; the parity test
(`test_migrated_ddl_equals_create_all`) keeps both equal. Nothing is copied or rewritten: every row
of every older table stays as it was.
"""

# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)
from datetime import datetime

import sqlalchemy as sa
from alembic import op

revision = "0077"
down_revision = "0065"
branch_labels = None
depends_on = None


KIND_CHECK = "ck_export_item_kind_values"
KINDS_BEFORE = ("unit", "volume", "bundle", "adopted")
KINDS_AFTER = (*KINDS_BEFORE, "opds_bundle")
# The triggers of `export_item` go with the table when it is rebuilt: they are written again, as
# they are in `infrastructure/schema/export.py`.
COVERAGE_CLEANUP = (
    "CREATE TRIGGER export_item_coverage_cleanup AFTER DELETE ON export_item "
    "BEGIN DELETE FROM unit_coverage "
    "WHERE carrier_type = 'export_item' AND carrier_id = OLD.id; END"
)


def _kinds(values: tuple[str, ...]) -> str:
    return '"kind" IN (' + ", ".join(f"'{v}'" for v in values) + ")"


def _rebuild_export_item(values: tuple[str, ...]) -> None:
    # SQLite batch mode (migrations run with `foreign_keys=OFF` then `foreign_key_check`, as 0008).
    # A view that reads the table (`reading_progress`) would make the rename fail: it is dropped
    # and written again, as it was, in the order it was made.
    views = (
        op.get_bind()
        .exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'view' AND sql LIKE '%export_item%' "
            "ORDER BY rowid"
        )
        .fetchall()
    )
    for name, _sql in views:
        op.execute(f"DROP VIEW {name}")
    created_at: sa.Column[datetime] = sa.Column(
        "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )  # reflected as `(CURRENT_TIMESTAMP)`: said again, as the schema writes it (see 0006)
    with op.batch_alter_table(
        "export_item", recreate="always", reflect_args=(created_at,)
    ) as batch:
        batch.drop_constraint(op.f(KIND_CHECK), type_="check")
        batch.create_check_constraint(op.f(KIND_CHECK), _kinds(values))
    op.execute("DROP TRIGGER IF EXISTS export_item_coverage_cleanup")
    op.execute(COVERAGE_CLEANUP)
    for _name, sql in views:
        op.execute(sql)


def upgrade() -> None:
    _rebuild_export_item(KINDS_AFTER)  # a carrier of its own, apart from the range's `bundle`
    op.execute(
        """
CREATE TABLE bundle_partition (
	edition_id VARCHAR(26) NOT NULL,
	part_index INTEGER NOT NULL,
	first_unit_id VARCHAR(26),
	export_item_id VARCHAR(26) NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_bundle_partition PRIMARY KEY (edition_id, part_index),
	CONSTRAINT ck_bundle_partition_first_unit_id_ulid CHECK (length("first_unit_id") = 26 AND "first_unit_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_bundle_partition_export_item_id_ulid CHECK (length("export_item_id") = 26 AND "export_item_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_bundle_partition_part_index_min CHECK ("part_index" >= 1),
	CONSTRAINT uq_bundle_partition_export_item_id UNIQUE (export_item_id),
	CONSTRAINT fk_bundle_partition_edition_id_edition FOREIGN KEY(edition_id) REFERENCES edition (id) ON DELETE CASCADE,
	CONSTRAINT fk_bundle_partition_export_item_id_export_item FOREIGN KEY(export_item_id) REFERENCES export_item (id) ON DELETE CASCADE
)
"""
    )
    op.execute(
        """
CREATE TABLE bundle_version (
	id VARCHAR(26) NOT NULL,
	edition_id VARCHAR(26) NOT NULL,
	part_index INTEGER NOT NULL,
	profile_user_id VARCHAR(26) NOT NULL,
	number INTEGER NOT NULL,
	content_fp VARCHAR NOT NULL,
	koreader_md5 VARCHAR NOT NULL,
	state_key VARCHAR NOT NULL,
	size BIGINT NOT NULL,
	entry_count INTEGER NOT NULL,
	page_count INTEGER NOT NULL,
	member_count INTEGER NOT NULL,
	excluded JSON DEFAULT '{}' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	last_used_at DATETIME,
	purged_at DATETIME,
	CONSTRAINT pk_bundle_version PRIMARY KEY (id),
	CONSTRAINT ck_bundle_version_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_bundle_version_number_min CHECK ("number" >= 1),
	CONSTRAINT ck_bundle_version_content_fp_hex64 CHECK ("content_fp" IS NULL OR (length("content_fp") = 64 AND "content_fp" NOT GLOB '*[^0-9a-f]*')),
	CONSTRAINT ck_bundle_version_state_key_hex64 CHECK ("state_key" IS NULL OR (length("state_key") = 64 AND "state_key" NOT GLOB '*[^0-9a-f]*')),
	CONSTRAINT ck_bundle_version_koreader_md5_md5_prefixed CHECK (length("koreader_md5") = 36 AND "koreader_md5" GLOB 'md5:*' AND substr("koreader_md5", 5) NOT GLOB '*[^0-9a-f]*'),
	CONSTRAINT ck_bundle_version_excluded_object CHECK (json_valid("excluded") AND json_type("excluded") = 'object'),
	CONSTRAINT uq_bundle_version_edition_id_part_index_profile_user_id_number UNIQUE (edition_id, part_index, profile_user_id, number),
	CONSTRAINT fk_bundle_version_edition_id_edition FOREIGN KEY(edition_id) REFERENCES edition (id) ON DELETE CASCADE,
	CONSTRAINT fk_bundle_version_profile_user_id_user FOREIGN KEY(profile_user_id) REFERENCES user (id) ON DELETE CASCADE
)
"""
    )
    op.execute("CREATE INDEX ix_bundle_version_profile ON bundle_version (profile_user_id)")
    op.execute(
        "CREATE INDEX ix_bundle_version_part ON bundle_version (edition_id, part_index, profile_user_id)"
    )
    op.execute(
        """
CREATE TABLE bundle_member (
	version_id VARCHAR(26) NOT NULL,
	rank INTEGER NOT NULL,
	export_item_id VARCHAR(26),
	unit_id VARCHAR(26),
	chapter_label VARCHAR NOT NULL,
	content_fp VARCHAR,
	pages_digest VARCHAR,
	first_page INTEGER NOT NULL,
	page_count INTEGER NOT NULL,
	page_map_state VARCHAR DEFAULT 'candidate' NOT NULL,
	CONSTRAINT pk_bundle_member PRIMARY KEY (version_id, rank),
	CONSTRAINT ck_bundle_member_page_map_state_values CHECK ("page_map_state" IN ('candidate', 'unverified')),
	CONSTRAINT ck_bundle_member_rank_min CHECK ("rank" >= 1),
	CONSTRAINT ck_bundle_member_pages_positive CHECK ("first_page" >= 1 AND "page_count" >= 0),
	CONSTRAINT ck_bundle_member_content_fp_hex64 CHECK ("content_fp" IS NULL OR (length("content_fp") = 64 AND "content_fp" NOT GLOB '*[^0-9a-f]*')),
	CONSTRAINT ck_bundle_member_pages_digest_hex64 CHECK ("pages_digest" IS NULL OR (length("pages_digest") = 64 AND "pages_digest" NOT GLOB '*[^0-9a-f]*')),
	CONSTRAINT fk_bundle_member_version_id_bundle_version FOREIGN KEY(version_id) REFERENCES bundle_version (id) ON DELETE CASCADE,
	CONSTRAINT fk_bundle_member_export_item_id_export_item FOREIGN KEY(export_item_id) REFERENCES export_item (id) ON DELETE SET NULL,
	CONSTRAINT fk_bundle_member_unit_id_unit FOREIGN KEY(unit_id) REFERENCES unit (id) ON DELETE SET NULL
)
"""
    )
    op.execute("CREATE INDEX ix_bundle_member_item ON bundle_member (export_item_id)")
    op.execute(
        "CREATE TRIGGER export_item_wipe_bundle_members BEFORE DELETE ON export_item BEGIN "
        "UPDATE bundle_member SET content_fp = NULL, pages_digest = NULL "
        "WHERE export_item_id = OLD.id; END"
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER export_item_wipe_bundle_members")
    op.drop_table("bundle_member")
    op.drop_table("bundle_version")
    op.drop_table("bundle_partition")
    op.execute("DELETE FROM export_item WHERE kind = 'opds_bundle'")
    _rebuild_export_item(KINDS_BEFORE)
