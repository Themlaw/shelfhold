"""`tombstone_lost_kid`: the kids covered by the "lost keyring" waiver (PLAN-M1a L33).

EF-BIB-054 (5) and R62: until the admin types `tombstone accept-lost-keyring`, a journal line whose
kid is registered but absent from the keyring blocks the reacquisition of its scope; after it, the
line is inert. The waiver names the kids it covers, so a key lost later is not waived silently.
Append-only, like `tombstone_kid` it references.
"""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

TRIGGERS = {
    "tombstone_lost_kid_no_update": (
        "CREATE TRIGGER tombstone_lost_kid_no_update "
        "BEFORE UPDATE ON tombstone_lost_kid "
        "BEGIN SELECT RAISE(ABORT, 'tombstone_lost_kid is append-only'); END"
    ),
    "tombstone_lost_kid_no_delete": (
        "CREATE TRIGGER tombstone_lost_kid_no_delete "
        "BEFORE DELETE ON tombstone_lost_kid "
        "BEGIN SELECT RAISE(ABORT, 'tombstone_lost_kid is append-only'); END"
    ),
}


def upgrade() -> None:
    op.create_table(
        "tombstone_lost_kid",
        sa.Column("kid", sa.String(), nullable=False),
        sa.Column("actor_id", sa.String(), nullable=True),
        sa.Column(
            "accepted_at", sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["kid"], ["tombstone_kid.kid"], name=op.f("fk_tombstone_lost_kid_kid_tombstone_kid")
        ),
        sa.PrimaryKeyConstraint("kid", name=op.f("pk_tombstone_lost_kid")),
    )
    for statement in TRIGGERS.values():
        op.execute(statement)


def downgrade() -> None:
    for name in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {name}")
    op.drop_table("tombstone_lost_kid")
