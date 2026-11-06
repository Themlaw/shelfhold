"""Follow notices and one active binding per upstream series (L42 review).

`follow_notice` keeps what the follow cycle has told (so a new process does not tell it again) and
what waits for the admin. The partial unique index refuses a second active binding on one
`source_item`; a database that already holds such a pair keeps the oldest binding active and sets
the others aside (`disabled_reason = 'manual'`), so the index can be built.
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "follow_notice",
        sa.Column("kind", sa.String(), nullable=False),
        sa.Column("key_hash", sa.String(), nullable=False),
        sa.Column("subject", sa.String(), nullable=False, server_default=""),
        sa.Column("told_at", sa.String(), nullable=False),
        sa.PrimaryKeyConstraint("kind", "key_hash", name=op.f("pk_follow_notice")),
        sa.CheckConstraint(
            "\"kind\" IN ('tombstoned', 'awaiting_shelf', 'pending_pairing', 'pairing_declined')",
            name=op.f("ck_follow_notice_kind_values"),
        ),
    )
    op.execute(
        "UPDATE source_binding SET active = 0, disabled_reason = 'manual' "
        "WHERE active AND id NOT IN ("
        "SELECT min(id) FROM source_binding WHERE active GROUP BY source_item_id)"
    )
    op.create_index(
        "uq_source_binding_active_source_item",
        "source_binding",
        ["source_item_id"],
        unique=True,
        sqlite_where=sa.text('"active"'),
    )


def downgrade() -> None:
    op.drop_index("uq_source_binding_active_source_item", table_name="source_binding")
    op.drop_table("follow_notice")
