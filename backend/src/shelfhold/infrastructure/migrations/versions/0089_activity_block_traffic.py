"""The blocklist and the traffic counter (M4 L29, EF-INT-019, EF-INT-020).

- `activity_block`: a chapter a source must not bring back, by `(source_key, remote_unit_id)`. It
  goes with nothing: a deleted account leaves `created_by` empty (`ON DELETE SET NULL`), never the
  block;
- `traffic_counter`: the requests and bytes the Fetcher carried, per source and per UTC day. No
  address, no host, no path.

Nothing existing is touched; reversible.

Number: 0089 follows 0088 (L21a), the head of main when this lot was frozen.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0089"
down_revision = "0088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE activity_block (
	id VARCHAR(26) NOT NULL,
	source_key VARCHAR NOT NULL,
	remote_unit_id VARCHAR NOT NULL,
	reason VARCHAR NOT NULL,
	created_by VARCHAR(26),
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	proposed_count INTEGER DEFAULT 0 NOT NULL,
	last_proposed_at DATETIME,
	CONSTRAINT pk_activity_block PRIMARY KEY (id),
	CONSTRAINT ck_activity_block_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_activity_block_reason_values CHECK ("reason" IN ('bad_quality', 'wrong_content', 'duplicate', 'other')),
	CONSTRAINT ck_activity_block_proposed_count_min CHECK ("proposed_count" >= 0),
	CONSTRAINT ck_activity_block_source_and_unit_named CHECK ("source_key" <> '' AND "remote_unit_id" <> ''),
	CONSTRAINT uq_activity_block_source_key_remote_unit_id UNIQUE (source_key, remote_unit_id),
	CONSTRAINT fk_activity_block_created_by_user FOREIGN KEY(created_by) REFERENCES user (id) ON DELETE SET NULL
)
"""
    )
    op.execute(
        """
CREATE TABLE traffic_counter (
	day DATE NOT NULL,
	source_key VARCHAR NOT NULL,
	requests INTEGER DEFAULT 0 NOT NULL,
	bytes_in BIGINT DEFAULT 0 NOT NULL,
	CONSTRAINT pk_traffic_counter PRIMARY KEY (day, source_key),
	CONSTRAINT ck_traffic_counter_source_named CHECK ("source_key" <> ''),
	CONSTRAINT ck_traffic_counter_requests_min CHECK ("requests" >= 0),
	CONSTRAINT ck_traffic_counter_bytes_in_min CHECK ("bytes_in" >= 0)
)
"""
    )


def downgrade() -> None:
    op.drop_table("traffic_counter")
    op.drop_table("activity_block")
