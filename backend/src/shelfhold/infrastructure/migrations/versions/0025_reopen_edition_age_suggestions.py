"""Reopen the Edition age suggestions that rating a Work rejected (cross-cutting review 2026-11-11).

Until this fix `RatingService.rate(work)` rejected every open age suggestion above the rated age,
Editions' included, without any surface showing them: an Edition a sub-source flagged X could enter
the library of the children. The code now leaves them open (only `rate_edition` rules on them).
This data migration puts back, as `open`, the `rejected` and the `accepted` suggestions of Editions
that nobody rated on their own (`edition.age_rating` is null: `rate_edition` always sets it, so a
ruling there was a decision), without comparing them with the Work's age: the old code accepted an
X suggestion when the Work was rated X, and the Work lowered later would let the Edition in. A
suggestion that does not exceed the age blocks nothing (the admission ignores it). No other row is
touched, and nothing can be undone: a downgrade keeps the rows open (they cannot be told apart from
the ones that always were).
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE age_suggestion SET state = 'open'
            WHERE state IN ('rejected', 'accepted') AND entity_type = 'edition'
              AND EXISTS (
                SELECT 1 FROM edition e
                WHERE e.id = age_suggestion.entity_id AND e.age_rating IS NULL
              )
            """
        )
    )


def downgrade() -> None:
    """Data only: the reopened suggestions stay open."""
