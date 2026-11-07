"""D19 (PLAN-M1b L04): who asked for a download, and who may see it (EF-USR-016).

Two nullable columns on `download_request`: `visibility` (`private` | `household` | `ops`, checked
by `activity_view`, which hides any other value) and `requester_user_id`. Existing rows keep NULL:
an unowned request is an operation, seen by the administrator only. No data is rewritten.
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("download_request", sa.Column("visibility", sa.String(), nullable=True))
    op.add_column("download_request", sa.Column("requester_user_id", sa.String(26), nullable=True))


def downgrade() -> None:
    op.drop_column("download_request", "requester_user_id")
    op.drop_column("download_request", "visibility")
