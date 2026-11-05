"""One open `download_request` per Variant (PLAN-M1a L18, review of PR #47).

Two follow cycles that overlap, or a retry racing a new discovery, must not open two tickets for
the same chapter. The partial unique index makes the database refuse the second one. Taken with a
migration token granted for L18; no row is rewritten (M1a is unreleased, so no duplicate exists).
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

INDEX = "uq_download_request_open_variant"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "download_request",
        ["variant_id"],
        unique=True,
        sqlite_where=sa.text("\"state\" IN ('requested', 'downloaded')"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="download_request")
