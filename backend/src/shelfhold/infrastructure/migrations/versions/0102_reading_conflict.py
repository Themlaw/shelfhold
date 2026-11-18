"""The divergence of two devices on one carrier (M5 L07, EF-LEC-007, D8): proposed once to the reader.

`reading_conflict` keeps identifiers and the reader's choice only (the positions themselves are the
history). It belongs to one user and goes with that user. At most one open proposal per user and
carrier (a partial unique index): once settled, or overtaken by a later position (`stale`), a later
divergence may be proposed again. Nothing
existing is touched; reversible.

Number: 0102 is the number the plan reserved for L07. `down_revision` is 0100 (the device registry of L02, the head of main),
re-chained when main was merged into the branch.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0102"
down_revision = "0100"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE reading_conflict (
	id VARCHAR(26) NOT NULL,
	user_id VARCHAR(26) NOT NULL,
	carrier_type VARCHAR NOT NULL,
	carrier_id VARCHAR(26) NOT NULL,
	winner_position_id VARCHAR(26) NOT NULL,
	loser_position_id VARCHAR(26) NOT NULL,
	choice VARCHAR,
	created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	resolved_at DATETIME,
	CONSTRAINT pk_reading_conflict PRIMARY KEY (id),
	CONSTRAINT ck_reading_conflict_id_ulid CHECK (length("id") = 26 AND "id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_reading_conflict_carrier_id_ulid CHECK (length("carrier_id") = 26 AND "carrier_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_reading_conflict_winner_position_id_ulid CHECK (length("winner_position_id") = 26 AND "winner_position_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_reading_conflict_loser_position_id_ulid CHECK (length("loser_position_id") = 26 AND "loser_position_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_reading_conflict_carrier_type_values CHECK ("carrier_type" IN ('export_item', 'content')),
	CONSTRAINT ck_reading_conflict_choice_values CHECK ("choice" IN ('winner', 'loser', 'stale')),
	CONSTRAINT ck_reading_conflict_choice_with_its_date CHECK (("choice" IS NULL) = ("resolved_at" IS NULL)),
	CONSTRAINT fk_reading_conflict_user_id_user FOREIGN KEY(user_id) REFERENCES user (id) ON DELETE CASCADE
)
"""
    )
    op.execute(
        'CREATE UNIQUE INDEX ux_reading_conflict_open ON reading_conflict (user_id, carrier_type, carrier_id) WHERE "resolved_at" IS NULL'
    )


def downgrade() -> None:
    op.execute("DROP INDEX ux_reading_conflict_open")
    op.execute("DROP TABLE reading_conflict")
