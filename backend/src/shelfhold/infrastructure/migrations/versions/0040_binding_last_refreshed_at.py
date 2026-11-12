"""`source_binding.last_refreshed_at`: when the source was last asked to refresh the series.

The adaptive cadence (PLAN-M2 L29, EF-SRC-038) ignores a "check now" on a source that was asked
less than `follow.check_min` ago, so the instant of the last refresh is written down. NULL =
never asked. No row is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("source_binding", sa.Column("last_refreshed_at", sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column("source_binding", "last_refreshed_at")
