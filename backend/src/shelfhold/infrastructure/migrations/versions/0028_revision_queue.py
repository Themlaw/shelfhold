"""Pending revisions carry what the Kavita API is asked to set (PLAN-M2 L17, EF-BIB-043).

`revision_pending.field` and `value`: the metadata field a `metadata_only` revision changed and
its new value; `applied_via`: the door a resolved row went through (`kavita_api`, `rewrite`, or
`forced_rewrite` for a revision the suspect-revision guard had quarantined, EF-BIB-024; the code
keeps that list, no CHECK). No row is
touched: the revisions accumulated before M2 are queued by `revisions list` the first time it
runs, from `variant_revision`, never rewritten.
"""

import sqlalchemy as sa
from alembic import op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # plain ADD COLUMN: no table rebuild, the rows and the DDL of the rest stay as they are
    op.add_column("revision_pending", sa.Column("field", sa.String(), nullable=True))
    op.add_column("revision_pending", sa.Column("value", sa.String(), nullable=True))
    op.add_column("revision_pending", sa.Column("applied_via", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("revision_pending", "applied_via")
    op.drop_column("revision_pending", "value")
    op.drop_column("revision_pending", "field")
