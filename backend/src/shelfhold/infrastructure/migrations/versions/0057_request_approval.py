"""What an approval records on a request, and the memo of a remote result (M3 L35, EF-USR-010).

`request`: `visibility` (what `effective_visibility` said when it was made: `private` or
`household`), the Work and Edition it was resolved to at approval, the shelf proposed, who
decided and when, the age chosen at approval (a child's request: applied to the Work when it
appears), and when the book arrived. `pending_source_item.memo`: what the search knew of a remote
result, so that the approval acts on the book and not on a search page.

Nothing is rewritten: a row made before this migration reads as private and undecided. The new
identifiers are plain columns, not foreign keys (a plain ADD COLUMN; a purge removes the Work, the
request only keeps a number that no longer leads anywhere).
"""

import sqlalchemy as sa
from alembic import op

revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request",
        sa.Column("visibility", sa.String(), nullable=False, server_default="private"),
    )
    for name in ("work_id", "edition_id", "shelf_id"):
        op.add_column("request", sa.Column(name, sa.String(26), nullable=True))
    op.add_column("request", sa.Column("decided_by", sa.String(), nullable=True))
    op.add_column("request", sa.Column("decided_at", sa.DateTime(), nullable=True))
    op.add_column("request", sa.Column("approved_age", sa.Integer(), nullable=True))
    op.add_column(
        "request", sa.Column("approved_x", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("request", sa.Column("arrived_at", sa.DateTime(), nullable=True))
    op.create_index("ix_request_work", "request", ["work_id"])
    op.add_column("pending_source_item", sa.Column("memo", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("pending_source_item", "memo")
    op.drop_index("ix_request_work", table_name="request")
    for name in (
        "arrived_at", "approved_x", "approved_age", "decided_at", "decided_by",
        "shelf_id", "edition_id", "work_id", "visibility",
    ):  # fmt: skip
        op.drop_column("request", name)
