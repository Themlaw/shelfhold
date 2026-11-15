"""What was pushed of each profile's read state to a Suwayomi series (M4 L17, EF-SRC-026).

One table, `read_state_push_note`, per owner profile, binding and chapter: the state last pushed
(`read` or `unread`) and when. It is the cursor of the push (the difference between the
`reading_progress` view and these notes is what is sent). It goes with the profile and with the
binding (`ON DELETE CASCADE`). Nothing existing is touched; reversible.

Number: 0079 is that of L23 (#293), the head of main when this lot was rebased.
"""

import sqlalchemy as sa
from alembic import op

revision = "0080"
down_revision = "0079"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "read_state_push_note",
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("source_binding_id", sa.String(length=26), nullable=False),
        sa.Column("remote_unit_id", sa.String(), nullable=False),
        sa.Column("unit_id", sa.String(length=26), nullable=False),
        sa.Column("state", sa.String(), nullable=False),
        sa.Column("pushed_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "\"state\" IN ('read', 'unread')",
            name=op.f("ck_read_state_push_note_state_values"),
        ),
        sa.CheckConstraint(
            'length("unit_id") = 26 AND "unit_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_read_state_push_note_unit_id_ulid"),
        ),
        sa.ForeignKeyConstraint(
            ["source_binding_id"],
            ["source_binding.id"],
            name=op.f("fk_read_state_push_note_source_binding_id_source_binding"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_read_state_push_note_user_id_user"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "user_id", "source_binding_id", "remote_unit_id", name=op.f("pk_read_state_push_note")
        ),
    )
    op.create_index(
        "ix_read_state_push_note_binding", "read_state_push_note", ["source_binding_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_read_state_push_note_binding", table_name="read_state_push_note")
    op.drop_table("read_state_push_note")
