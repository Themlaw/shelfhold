"""What `?since=` needs of the database (M3 L11, EF-INT-001).

- `work.created_seq`: the sequence of the `event` table when the Work was created, set by a trigger
  that also moves the sequence by one. A token issued right after the creation holds exactly that
  value, and the Work then existed at the token (`created_seq <= token`); a Work created later has
  a greater value, because its creation moved the sequence past every token already issued. The
  removals of a list never name a Work that did not exist at the token. Works that exist now are
  0: older than any token.
- `instance.events_purged_through`: the highest event id the retention purged; a token under it is
  `resync_required`. 0 until the first purge.
"""

import sqlalchemy as sa
from alembic import op

revision = "0059"
down_revision = "0057"
branch_labels = None
depends_on = None

TRIGGER = (
    "CREATE TRIGGER work_created_seq "
    "AFTER INSERT ON work BEGIN "
    "INSERT INTO sqlite_sequence (name, seq) SELECT 'event', 0 WHERE NOT EXISTS "
    "(SELECT 1 FROM sqlite_sequence WHERE name = 'event'); "
    "UPDATE sqlite_sequence SET seq = seq + 1 WHERE name = 'event'; "
    "UPDATE work SET created_seq = (SELECT seq FROM sqlite_sequence WHERE name = 'event') "
    "WHERE id = NEW.id; END"
)


def upgrade() -> None:
    op.add_column(
        "work", sa.Column("created_seq", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "instance",
        sa.Column("events_purged_through", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(TRIGGER)


def downgrade() -> None:
    op.execute("DROP TRIGGER work_created_seq")
    op.drop_column("instance", "events_purged_through")
    op.drop_column("work", "created_seq")
