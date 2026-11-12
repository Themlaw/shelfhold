"""`contributor.died_for_france`: the status "mort pour la France" (M2 L15b, EF-SRC-024).

It adds 30 years to the French term, and nothing recorded it. NULL = unknown, which is what every
existing row keeps: the rights review reads it on the cautious side. No row is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0047"
down_revision = "0046"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("contributor", sa.Column("died_for_france", sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column("contributor", "died_for_france")
