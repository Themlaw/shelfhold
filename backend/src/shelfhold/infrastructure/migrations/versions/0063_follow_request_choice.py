"""`follow_request.mode`, `from_number`, `shelf_id`: what the add sheet chose (M3 L42, EF-UX-006).

A follow asks the connected service to keep a series, and the series becomes an Edition later, in
the discovery of a cycle. The choice made on the sheet (which Units, from which number, on which
shelf) is written with the request so that the discovery applies it. NULL mode: the series was
added without a choice (the host CLI, the service's own library) and nothing changes for it.
"""

import sqlalchemy as sa
from alembic import op

revision = "0063"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("follow_request") as batch:
        batch.add_column(sa.Column("mode", sa.String(), nullable=True))
        batch.add_column(sa.Column("from_number", sa.String(), nullable=True))
        batch.add_column(sa.Column("shelf_id", sa.String(26), nullable=True))
        batch.create_foreign_key(
            "fk_follow_request_shelf_id_shelf", "shelf", ["shelf_id"], ["id"], ondelete="SET NULL"
        )
        batch.create_check_constraint(
            "mode_values", "\"mode\" IN ('all', 'from_number', 'new_only')"
        )


def downgrade() -> None:
    with op.batch_alter_table("follow_request") as batch:
        batch.drop_constraint("mode_values", type_="check")
        batch.drop_constraint("fk_follow_request_shelf_id_shelf", type_="foreignkey")
        batch.drop_column("shelf_id")
        batch.drop_column("from_number")
        batch.drop_column("mode")
