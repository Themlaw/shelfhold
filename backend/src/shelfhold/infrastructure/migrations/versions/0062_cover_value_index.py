"""The cover filter of a thumbnail reaches its row by the value (M3 L24, EF-BIB-020).

Every thumbnail request asks whether an Edition the reader may see points at a cover, that is, it
looks up `metadata_field` by the cover reference. A partial index on the value of the Editions'
`cover` field only (a few dozen bytes a row, no description or title in it) turns the scan of every
field of every Edition, sixty times a screen, into one seek.
"""

import sqlalchemy as sa
from alembic import op

revision = "0062"
down_revision = "0059"
branch_labels = None
depends_on = None

INDEX = "ix_metadata_field_edition_cover"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "metadata_field",
        ["value"],
        sqlite_where=sa.text("\"entity_type\" = 'edition' AND \"field\" = 'cover'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="metadata_field")
