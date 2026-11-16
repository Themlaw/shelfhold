"""The slice of a series block (M4 L49b, ADR-0053 §1 and §8).

One table, `bundle_setting`: for an Edition, the number of chapters (1 to 256) a new part of its
block may hold. It goes with its Edition (`ON DELETE CASCADE`). Nothing existing is touched, and no
written boundary moves; reversible.

Number: 0081 is that of L04 (#299), the head of main when this lot was rebased.
"""
# ruff: noqa: E501  (the DDL is the one SQLAlchemy writes, kept as it is)

from alembic import op

revision = "0082"
down_revision = "0081"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
CREATE TABLE bundle_setting (
	edition_id VARCHAR(26) NOT NULL,
	slice_chapters INTEGER NOT NULL,
	updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
	CONSTRAINT pk_bundle_setting PRIMARY KEY (edition_id),
	CONSTRAINT ck_bundle_setting_edition_id_ulid CHECK (length("edition_id") = 26 AND "edition_id" NOT GLOB '*[^0-9A-HJKMNP-TV-Z]*'),
	CONSTRAINT ck_bundle_setting_slice_chapters_range CHECK ("slice_chapters" >= 1 AND "slice_chapters" <= 256),
	CONSTRAINT fk_bundle_setting_edition_id_edition FOREIGN KEY(edition_id) REFERENCES edition (id) ON DELETE CASCADE
)
"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE bundle_setting")
