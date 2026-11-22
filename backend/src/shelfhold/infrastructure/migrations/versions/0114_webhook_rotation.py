"""The rotation window of a webhook's secret (M6 L23a, EF-INT-005, D23): `rotated_at`.

The previous secret stays in the sealed document (no new envelope: the export exclusion, the
write guard and the key rotation keep covering it); the column says when the rotation happened,
and a delivery signs with both secrets for 24 h after it. Reversible: the downgrade drops the
column, and a rotated subscription then signs with its current secret only, which every receiver
that moved over already has.

0096 builds the table from the schema itself, so a database created since the schema gained the
column already has it when this migration runs: the column is added only where it is missing (a
database created before). The downgrade drops it wherever it is.

Number: 0114 is the one the launch of L23a names (0109 to 0113 are held by other lots).
`down_revision` is the head of main (0108, M5 L20b); the chain is re-linked on the merge,
whichever of the held lots goes first.
"""

import sqlalchemy as sa
from alembic import op

from shelfhold.infrastructure.schema._base import UtcDateTime

revision = "0114"
down_revision = "0108"
branch_labels = None
depends_on = None

_TABLE = "webhook_subscription"
_COLUMN = "rotated_at"


def _columns() -> set[str]:
    return {row[1] for row in op.get_bind().execute(sa.text(f"PRAGMA table_info({_TABLE})"))}


def upgrade() -> None:
    if _COLUMN not in _columns():  # a plain ADD COLUMN: no table rebuild, the rows stay
        op.add_column(_TABLE, sa.Column(_COLUMN, UtcDateTime(), nullable=True))


def downgrade() -> None:
    if _COLUMN in _columns():
        op.drop_column(_TABLE, _COLUMN)
