"""Collections and reading lists (M4 L18a, EF-BIB-016).

Three tables: `collection` (the household's, or a profile's own), `collection_item` (a Work in an
order) and `collection_remote` (what a destination called a collection: L18b fills it). Each goes
with its owner, its collection and its Work (`ON DELETE CASCADE`). Nothing existing is touched;
reversible.

Number: 0086 follows 0085 (L53), the head of main when this lot was rebased.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0086"
down_revision = "0085"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE collection (
	id VARCHAR(26) NOT NULL,
	kind VARCHAR NOT NULL,
	name VARCHAR NOT NULL,
	owner_id VARCHAR(26),
	source VARCHAR DEFAULT 'manual' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_collection PRIMARY KEY (id),
	CONSTRAINT ck_collection_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_collection_kind_values CHECK ("kind" IN ('collection', 'reading_list')),
	CONSTRAINT ck_collection_source_values CHECK ("source" IN ('manual', 'cbl')),
	CONSTRAINT ck_collection_name_length CHECK (length("name") BETWEEN 1 AND 200),
	CONSTRAINT fk_collection_owner_id_user FOREIGN KEY(owner_id) REFERENCES user (id) ON DELETE CASCADE
)
"""
    )
    op.execute(
        """
CREATE INDEX ix_collection_owner ON collection (owner_id)
"""
    )
    op.execute(
        """
CREATE TABLE collection_item (
	id VARCHAR(26) NOT NULL,
	collection_id VARCHAR(26) NOT NULL,
	position INTEGER NOT NULL,
	work_id VARCHAR(26) NOT NULL,
	number VARCHAR,
	added_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_collection_item PRIMARY KEY (id),
	CONSTRAINT ck_collection_item_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_collection_item_position_min CHECK ("position" >= 0),
	CONSTRAINT ck_collection_item_number_length CHECK ("number" IS NULL OR length("number") BETWEEN 1 AND 40),
	CONSTRAINT uq_collection_item_place UNIQUE (collection_id, position),
	CONSTRAINT fk_collection_item_collection_id_collection FOREIGN KEY(collection_id) REFERENCES collection (id) ON DELETE CASCADE,
	CONSTRAINT fk_collection_item_work_id_work FOREIGN KEY(work_id) REFERENCES work (id) ON DELETE CASCADE
)
"""
    )
    op.execute(
        """
CREATE INDEX ix_collection_item_work ON collection_item (work_id)
"""
    )
    op.execute(
        """
CREATE TABLE collection_remote (
	collection_id VARCHAR(26) NOT NULL,
	target_id VARCHAR(26) NOT NULL,
	remote_id VARCHAR NOT NULL,
	synced_at DATETIME,
	CONSTRAINT pk_collection_remote PRIMARY KEY (collection_id, target_id),
	CONSTRAINT ck_collection_remote_remote_id_named CHECK ("remote_id" <> ''),
	CONSTRAINT fk_collection_remote_collection_id_collection FOREIGN KEY(collection_id) REFERENCES collection (id) ON DELETE CASCADE,
	CONSTRAINT fk_collection_remote_target_id_export_target FOREIGN KEY(target_id) REFERENCES export_target (id) ON DELETE CASCADE
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE collection_remote")
    op.execute("DROP TABLE collection_item")
    op.execute("DROP TABLE collection")
