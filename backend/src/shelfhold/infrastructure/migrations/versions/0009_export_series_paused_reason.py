"""A series paused by a foreign writer (PLAN-M1a L32, EF-EXP-043).

`export_series.paused_reason` is null for a series that exports normally and `foreign_writer`
once the detection has paused it; `series resume` clears it. Taken with a migration token for
L32; no row is rewritten.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("export_series", sa.Column("paused_reason", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("export_series") as batch:
        batch.drop_column("paused_reason")
