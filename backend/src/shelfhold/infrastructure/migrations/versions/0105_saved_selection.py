"""Saved selections (M5 L21, EF-BIB-017): `saved_selection`, a name and a stored query.

The query is one JSON object of the closed grammar of `catalog.domain.selection_query` (the
application checks the grammar; the table holds the shape and a size of at most 2 KiB). It holds no
Work and no result: it is evaluated each time it is read, in the scope of the profile that reads it.
A selection belongs to one profile, read by her alone, and goes with her (`ON DELETE CASCADE`); a
Work or a shelf that leaves the catalogue changes nothing in it. Nothing existing is touched;
reversible.

Number: 0105 is the one PLAN-M5 reserved for this lot. `down_revision` is 0107, the head of main when it was merged.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0105"
down_revision = "0107"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE saved_selection (
	id VARCHAR(26) NOT NULL,
	owner_user_id VARCHAR(26) NOT NULL,
	name VARCHAR NOT NULL,
	query_json TEXT NOT NULL,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_saved_selection PRIMARY KEY (id),
	CONSTRAINT ck_saved_selection_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_saved_selection_owner_user_id_ulid CHECK (length("owner_user_id") = 26 AND "owner_user_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_saved_selection_name_length CHECK (length("name") BETWEEN 1 AND 200),
	CONSTRAINT ck_saved_selection_query_object CHECK (CASE WHEN json_valid("query_json") THEN json_type("query_json") = 'object' ELSE 0 END),
	CONSTRAINT ck_saved_selection_query_size CHECK (length(CAST("query_json" AS BLOB)) <= 2048),
	CONSTRAINT fk_saved_selection_owner_user_id_user FOREIGN KEY(owner_user_id) REFERENCES user (id) ON DELETE CASCADE
)
"""
    )
    op.execute("CREATE INDEX ix_saved_selection_owner ON saved_selection (owner_user_id)")


def downgrade() -> None:
    op.execute("DROP INDEX ix_saved_selection_owner")
    op.execute("DROP TABLE saved_selection")
