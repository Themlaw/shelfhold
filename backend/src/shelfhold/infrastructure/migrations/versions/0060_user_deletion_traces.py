"""What a deletion of a user must reach (M3 L08, EF-USR-013).

- `follow_request` and `download_request`: an `AFTER INSERT` trigger turns a `requester_user_id`
  that names no user into NULL. The column has no foreign key, so a request authenticated before a
  deletion and written after it kept the id for ever.
- `tombstone_lost_kid`: the table stays append-only, but one rewrite is allowed: `actor_id` takes
  an opaque `deleted:` token when the admin who typed the waiver is deleted. Nothing else moves.
"""

from alembic import op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None

TICKETS = ("download_request", "follow_request")

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


def _requester_trigger(table: str) -> str:
    return (
        f"CREATE TRIGGER {table}_requester_exists AFTER INSERT ON {table} "  # noqa: S608
        "WHEN NEW.requester_user_id IS NOT NULL "
        'AND NOT EXISTS (SELECT 1 FROM "user" WHERE id = NEW.requester_user_id) '
        f"BEGIN UPDATE {table} SET requester_user_id = NULL WHERE id = NEW.id; END"
    )


def upgrade() -> None:
    op.execute("DROP TRIGGER tombstone_lost_kid_no_update")
    op.execute(LOST_KID_UPDATE)
    for table in TICKETS:
        op.execute(_requester_trigger(table))


def downgrade() -> None:
    for table in TICKETS:
        op.execute(f"DROP TRIGGER {table}_requester_exists")
    op.execute("DROP TRIGGER tombstone_lost_kid_no_update")
    op.execute(LOST_KID_UPDATE_BEFORE)
