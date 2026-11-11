"""Reopen the Edition age suggestions that rating a Work rejected (cross-cutting review 2026-11-11).

Until this fix `RatingService.rate(work)` rejected every open age suggestion above the rated age,
Editions' included, without any surface showing them: an Edition a sub-source flagged X could enter
the library of the children. The code now leaves them open (only `rate_edition` rules on them).
This data migration puts back, as `open`, the rejected suggestions of Editions that nobody rated
on their own (`edition.age_rating` is null: `rate_edition` always sets it, so a rejection there
was a decision) and that are above the current rating of their Work. The Edition is blocked again
below 18+ until an admin rates it. No other row is touched, and nothing can be undone: a
downgrade keeps the rows open (they cannot be told apart from the ones that always were).
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE age_suggestion SET state = 'open'
            WHERE state = 'rejected' AND entity_type = 'edition'
              AND EXISTS (
                SELECT 1 FROM edition e JOIN work w ON w.id = e.work_id
                WHERE e.id = age_suggestion.entity_id
                  AND e.age_rating IS NULL AND e.age_x = 0
                  AND w.age_rating IS NOT NULL
                  AND (CASE WHEN age_suggestion.x = 1 THEN 99 ELSE age_suggestion.value END)
                      > (CASE WHEN w.age_x = 1 THEN 99 ELSE w.age_rating END)
              )
            """
        )
    )


def downgrade() -> None:
    """Data only: the reopened suggestions stay open."""
