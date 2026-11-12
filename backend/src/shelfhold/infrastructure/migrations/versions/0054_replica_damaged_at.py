"""`replica.damaged_at`: the integrity check read this copy and it failed (PLAN-M3 L23, EF-BIB-012).

An explicit mark, so that a damaged copy is never taken for an imported one (`independent` false
and never verified). NULL = healthy. No row is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("replica", sa.Column("damaged_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("replica", "damaged_at")
