"""M1b L19 (EF-SRC-029): the requests to follow a series of the connected service.

One new table, `follow_request`, keyed by the upstream series: its unique key is what keeps the
entry in the service's library to one, whatever the writers. No existing row is touched.
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

ULID = 'length("id") = 26 AND "id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\''


def upgrade() -> None:
    op.create_table(
        "follow_request",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("source_key", sa.String(), nullable=False),
        sa.Column("remote_id", sa.String(), nullable=False),
        sa.Column("state", sa.String(), server_default="approved", nullable=False),
        sa.Column("visibility", sa.String(), server_default="ops", nullable=False),
        sa.Column("requester_user_id", sa.String(length=26), nullable=True),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False
        ),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(ULID, name=op.f("ck_follow_request_id_ulid")),
        sa.CheckConstraint(
            "\"state\" IN ('approved', 'following', 'followed', 'unfollowed')",
            name=op.f("ck_follow_request_state_values"),
        ),
        sa.CheckConstraint(
            "\"visibility\" IN ('private', 'household', 'ops')",
            name=op.f("ck_follow_request_visibility_values"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_follow_request")),
        sa.UniqueConstraint(
            "source_key", "remote_id", name=op.f("uq_follow_request_source_key_remote_id")
        ),
    )


def downgrade() -> None:
    op.drop_table("follow_request")
