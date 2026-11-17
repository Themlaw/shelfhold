"""A bulk order (M4 L28, EF-BIB-023): the selection a preview froze, applied once by one job.

`bulk_order` keeps identifiers and a digest only (the Works chosen that waited, the age, the state
of the order, the job that applies it): no title, no secret. It belongs to one user and goes with
that user. Nothing existing is touched; reversible.

Number: 0098 follows 0097 (L35), the head of main when this lot was frozen.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0098"
down_revision = "0097"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE bulk_order (
	id VARCHAR(26) NOT NULL,
	kind VARCHAR NOT NULL,
	actor_id VARCHAR(26) NOT NULL,
	params JSON DEFAULT '{}' NOT NULL,
	work_ids JSON DEFAULT '[]' NOT NULL,
	digest VARCHAR NOT NULL,
	state VARCHAR DEFAULT 'previewed' NOT NULL,
	job_id VARCHAR(26),
	summary JSON DEFAULT '{}' NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	applied_at DATETIME,
	CONSTRAINT pk_bulk_order PRIMARY KEY (id),
	CONSTRAINT ck_bulk_order_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_bulk_order_job_id_ulid CHECK (length("job_id") = 26 AND "job_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_bulk_order_kind_values CHECK ("kind" IN ('rate')),
	CONSTRAINT ck_bulk_order_state_values CHECK ("state" IN ('previewed', 'queued', 'applied', 'stale', 'refused')),
	CONSTRAINT ck_bulk_order_params_object CHECK (json_valid("params") AND json_type("params") = 'object'),
	CONSTRAINT ck_bulk_order_work_ids_list CHECK (json_valid("work_ids") AND json_type("work_ids") = 'array'),
	CONSTRAINT ck_bulk_order_summary_object CHECK (json_valid("summary") AND json_type("summary") = 'object'),
	CONSTRAINT fk_bulk_order_actor_id_user FOREIGN KEY(actor_id) REFERENCES user (id) ON DELETE CASCADE
)
"""
    )
    op.execute("CREATE INDEX ix_bulk_order_actor ON bulk_order (actor_id)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_bulk_order_actor")
    op.execute("DROP TABLE bulk_order")
