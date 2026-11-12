"""`trash_item.deletion_id` and `trash_item.snapshot`: what a restore needs (M3 L25, EF-BIB-021).

A deletion of content (levels "remove from destinations" and "delete the content") moves files
into the bin of their root and forgets their rows. To put a file back exactly as it was, the bin
entry keeps what was forgotten: `snapshot` (the registry row of the copy and the record of the
export that served it, as JSON) and `deletion_id` (the run it belongs to, so that one restore
puts back one deletion). Both NULL for the bin entries of before: those can be swept but not
restored. No row is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("trash_item", sa.Column("deletion_id", sa.String(26), nullable=True))
    op.add_column("trash_item", sa.Column("snapshot", sa.JSON(none_as_null=True), nullable=True))
    op.create_index("ix_trash_item_deletion", "trash_item", ["deletion_id"])


def downgrade() -> None:
    op.drop_index("ix_trash_item_deletion", table_name="trash_item")
    op.drop_column("trash_item", "snapshot")
    op.drop_column("trash_item", "deletion_id")
