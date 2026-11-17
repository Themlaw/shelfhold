"""The migration of a series to another source (M4 L27, EF-BIB-014).

Two tables: `source_migration` (one proposed numbering correspondence, its state and the
fingerprints that bind it to the Edition it was made for) and `source_migration_row` (one line of
it: a number of the new source, the library number it takes, the Unit it lands on). Each goes with
its Edition (`ON DELETE CASCADE`). Nothing existing is touched; reversible.

Number: 0090 follows 0089 (the volume assembly), the head of main when this lot was rebased.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0090"
down_revision = "0089"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE source_migration (
	id VARCHAR(26) NOT NULL,
	edition_id VARCHAR(26) NOT NULL,
	work_id VARCHAR(26) NOT NULL,
	plugin_id VARCHAR NOT NULL,
	source_key VARCHAR NOT NULL,
	remote_id VARCHAR NOT NULL,
	memo JSON DEFAULT '{}' NOT NULL,
	memo_version INTEGER,
	replaces_binding_id VARCHAR(26),
	binding_id VARCHAR(26),
	state VARCHAR DEFAULT 'proposed' NOT NULL,
	number_offset INTEGER DEFAULT '0' NOT NULL,
	digest VARCHAR(64) NOT NULL,
	held_digest VARCHAR(64) NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	validated_at DATETIME,
	applied_at DATETIME,
	CONSTRAINT pk_source_migration PRIMARY KEY (id),
	CONSTRAINT ck_source_migration_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_source_migration_edition_id_ulid CHECK (length("edition_id") = 26 AND "edition_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_source_migration_state_values CHECK ("state" IN ('proposed', 'validated', 'applied', 'cancelled')),
	CONSTRAINT ck_source_migration_memo_object CHECK (json_valid("memo") AND json_type("memo") = 'object'),
	CONSTRAINT ck_source_migration_named CHECK ("plugin_id" <> '' AND "source_key" <> '' AND "remote_id" <> ''),
	CONSTRAINT ck_source_migration_offset_range CHECK ("number_offset" BETWEEN -100000 AND 100000),
	CONSTRAINT ck_source_migration_digests CHECK (length("digest") = 64 AND length("held_digest") = 64),
	CONSTRAINT ck_source_migration_applied_is_dated CHECK (("state" = 'applied') = ("applied_at" IS NOT NULL)),
	CONSTRAINT fk_source_migration_edition_id_edition FOREIGN KEY(edition_id) REFERENCES edition (id) ON DELETE CASCADE,
	CONSTRAINT fk_source_migration_work_id_work FOREIGN KEY(work_id) REFERENCES work (id) ON DELETE CASCADE,
	CONSTRAINT fk_source_migration_replaces_binding_id_source_binding FOREIGN KEY(replaces_binding_id) REFERENCES source_binding (id) ON DELETE SET NULL,
	CONSTRAINT fk_source_migration_binding_id_source_binding FOREIGN KEY(binding_id) REFERENCES source_binding (id) ON DELETE SET NULL
)
"""
    )
    op.execute("CREATE INDEX ix_source_migration_edition ON source_migration (edition_id)")
    op.execute(
        "CREATE UNIQUE INDEX uq_source_migration_open ON source_migration (edition_id, digest) "
        "WHERE \"state\" IN ('proposed', 'validated')"
    )
    op.execute(
        """
CREATE TABLE source_migration_row (
	migration_id VARCHAR(26) NOT NULL,
	place VARCHAR NOT NULL,
	number VARCHAR,
	source_order INTEGER DEFAULT '0' NOT NULL,
	target VARCHAR,
	target_order INTEGER DEFAULT '0' NOT NULL,
	target_place VARCHAR NOT NULL,
	unit_id VARCHAR(26),
	basis VARCHAR NOT NULL,
	CONSTRAINT pk_source_migration_row PRIMARY KEY (migration_id, place),
	CONSTRAINT ck_source_migration_row_migration_id_ulid CHECK (length("migration_id") = 26 AND "migration_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_source_migration_row_basis_values CHECK ("basis" IN ('same_number', 'offset', 'manual', 'new')),
	CONSTRAINT ck_source_migration_row_source_order_min CHECK ("source_order" >= 0),
	CONSTRAINT ck_source_migration_row_target_order_min CHECK ("target_order" >= 0),
	CONSTRAINT uq_source_migration_row_migration_id_target_place UNIQUE (migration_id, target_place),
	CONSTRAINT fk_source_migration_row_migration_id_source_migration FOREIGN KEY(migration_id) REFERENCES source_migration (id) ON DELETE CASCADE,
	CONSTRAINT fk_source_migration_row_unit_id_unit FOREIGN KEY(unit_id) REFERENCES unit (id) ON DELETE SET NULL
)
"""
    )
    op.execute("CREATE INDEX ix_source_migration_row_unit ON source_migration_row (unit_id)")


def downgrade() -> None:
    op.execute("DROP TABLE source_migration_row")
    op.execute("DROP TABLE source_migration")
