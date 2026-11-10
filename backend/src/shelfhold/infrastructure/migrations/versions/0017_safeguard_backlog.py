"""M1b L16 (EF-BIB-042 (4), EF-INT-023): what "Mettre à l'abri" left behind at its last cycle.

One new table, `safeguard_backlog`, with a single row (`store`): the Units and bytes that stayed
behind the per-cycle cap or the room of /store. No existing row is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "safeguard_backlog",
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("units", sa.Integer(), server_default="0", nullable=False),
        sa.Column("bytes", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("since", sa.DateTime(), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.CheckConstraint("\"name\" = 'store'", name=op.f("ck_safeguard_backlog_name_values")),
        sa.CheckConstraint('"units" >= 0', name=op.f("ck_safeguard_backlog_units_min")),
        sa.CheckConstraint('"bytes" >= 0', name=op.f("ck_safeguard_backlog_bytes_min")),
        sa.PrimaryKeyConstraint("name", name=op.f("pk_safeguard_backlog")),
    )


def downgrade() -> None:
    op.drop_table("safeguard_backlog")
