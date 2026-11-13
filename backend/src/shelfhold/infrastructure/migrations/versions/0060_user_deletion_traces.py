"""What a deletion of a user must reach (M3 L08, EF-USR-013).

`tombstone_lost_kid` stays append-only, but one rewrite is allowed: `actor_id` takes an opaque
`deleted:` token when the admin who typed the waiver is deleted. Nothing else moves.

(The tickets of downloads and follows are not covered by a trigger: one that names the `user` table
breaks the batch rebuild of that table. The writers check the requester in their own transaction:
`infrastructure/requesters.py`.)
"""

from alembic import op

revision = "0060"
down_revision = "0062"
branch_labels = None
depends_on = None

LOST_KID_UPDATE = (
    "CREATE TRIGGER tombstone_lost_kid_no_update BEFORE UPDATE ON tombstone_lost_kid "
    "WHEN NOT (COALESCE(NEW.actor_id LIKE 'deleted:%', 0) "
    "AND COALESCE(OLD.actor_id NOT LIKE 'deleted:%', 0) "
    "AND NEW.kid IS OLD.kid AND NEW.accepted_at IS OLD.accepted_at) "
    "BEGIN SELECT RAISE(ABORT, 'tombstone_lost_kid is append-only'); END"
)
LOST_KID_UPDATE_BEFORE = (
    "CREATE TRIGGER tombstone_lost_kid_no_update BEFORE UPDATE ON tombstone_lost_kid "
    "BEGIN SELECT RAISE(ABORT, 'tombstone_lost_kid is append-only'); END"
)


def upgrade() -> None:
    op.execute("DROP TRIGGER tombstone_lost_kid_no_update")
    op.execute(LOST_KID_UPDATE)


def downgrade() -> None:
    op.execute("DROP TRIGGER tombstone_lost_kid_no_update")
    op.execute(LOST_KID_UPDATE_BEFORE)
