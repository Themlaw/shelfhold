"""`job_view.window_end` and `job_view.cursor` (PLAN-M1a L12, ENF-OPS-014).

A window job has to stop at the end of the window it was queued in and to resume, the next night,
from where it stopped: the first needs that end (UTC), the second a cursor (JSON, owned by the
job). Taken with the migration token granted for L12. `job_view` is rebuilt by SQLite batch mode,
safe because migrations run with `foreign_keys=OFF` followed by `foreign_key_check` (L02); its
partial unique index is carried over by reflection. The revision is a frozen snapshot of the
metadata: `test_migrated_ddl_equals_create_all` keeps the two honest.
"""

from contextlib import AbstractContextManager
from datetime import datetime

import sqlalchemy as sa
from alembic import op
from alembic.operations.base import BatchOperations

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

CURSOR_CHECK = "ck_job_view_cursor_json"


def _rebuild() -> AbstractContextManager[BatchOperations]:
    """Reflection hands `CURRENT_TIMESTAMP` back in a form SQLite re-renders with parentheses."""
    created_at: sa.Column[datetime] = sa.Column(
        "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
    )
    return op.batch_alter_table("job_view", recreate="always", reflect_args=(created_at,))


def upgrade() -> None:
    with _rebuild() as batch:
        batch.add_column(sa.Column("window_end", sa.DateTime(), nullable=True))
        batch.add_column(sa.Column("cursor", sa.Text(), nullable=True))
        batch.create_check_constraint(op.f(CURSOR_CHECK), 'cursor IS NULL OR json_valid("cursor")')


def downgrade() -> None:
    with _rebuild() as batch:
        batch.drop_constraint(op.f(CURSOR_CHECK), type_="check")
        batch.drop_column("cursor")
        batch.drop_column("window_end")
