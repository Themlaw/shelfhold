"""`export_record.spine_count`: the length of the file served, for the suspect-revision guard.

The tail of a web-novel volume is replaced at the same path (PLAN-M2 L26, EF-BIB-040); the guard
of EF-BIB-024 compares the replacement with what is served, so the count of reading-order items of
the served file is written down when it is placed. NULL = unknown (the guard then says nothing).
No row is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0037"
down_revision = "0035"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("export_record", sa.Column("spine_count", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("export_record", "spine_count")
