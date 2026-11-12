"""`user.visibility`: what a profile's own requests and activity show to the others (M3 L09,
EF-USR-016).

`private` or `household`; NULL means "the default of the role" (`private` for an adult,
`household` for a child), so no row is touched and a profile whose role changes follows the new
default. The rule that a value is one of the two words, and that a child is never hidden from the
parents, lives in `identity.domain.visibility`; a value nobody defined is read as the default.
"""

import sqlalchemy as sa
from alembic import op

revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("user", sa.Column("visibility", sa.String(), nullable=True))


def downgrade() -> None:
    op.drop_column("user", "visibility")
