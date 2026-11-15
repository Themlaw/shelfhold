"""What the progress synchronisation with Kavita noted of each chapter (M4 L14b, EF-EXP-026).

One table, `progress_sync_note`, per account and Kavita chapter: the page Shelfhold last pushed
(the echo of its own push is never read back as a Kavita state), what it last saw, and when the
chapter was last synchronised. It goes with its account (`ON DELETE CASCADE`). Nothing existing is
touched; reversible.
"""

import sqlalchemy as sa
from alembic import op

revision = "0078"
down_revision = "0077"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "progress_sync_note",
        sa.Column("shelf_destination_id", sa.String(length=26), nullable=False),
        sa.Column("user_id", sa.String(length=26), nullable=False),
        sa.Column("kavita_chapter_id", sa.Integer(), nullable=False),
        sa.Column("account_id", sa.String(length=26), nullable=False),
        sa.Column("account_ref", sa.String(), nullable=False),
        sa.Column("carrier_id", sa.String(length=26), nullable=False),
        sa.Column("kavita_series_id", sa.Integer(), nullable=True),
        sa.Column("series_stamp", sa.String(), nullable=True),
        sa.Column("pushed_page", sa.Integer(), nullable=True),
        sa.Column("pushed_at", sa.DateTime(), nullable=True),
        sa.Column("seen_page", sa.Integer(), nullable=True),
        sa.Column("seen_modified", sa.String(), nullable=True),
        sa.Column("synced_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            'length("account_id") = 26 AND "account_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_progress_sync_note_account_id_ulid"),
        ),
        sa.CheckConstraint(
            'length("carrier_id") = 26 AND "carrier_id" NOT GLOB \'*[^0-9A-HJKMNP-TV-Z]*\'',
            name=op.f("ck_progress_sync_note_carrier_id_ulid"),
        ),
        sa.CheckConstraint(
            '"pushed_page" IS NULL OR "pushed_page" >= 0',
            name=op.f("ck_progress_sync_note_pushed_page_min"),
        ),
        sa.CheckConstraint(
            '"seen_page" IS NULL OR "seen_page" >= 0',
            name=op.f("ck_progress_sync_note_seen_page_min"),
        ),
        sa.ForeignKeyConstraint(
            ["user_id", "shelf_destination_id"],
            ["destination_account.user_id", "destination_account.shelf_destination_id"],
            name=op.f("fk_progress_sync_note_user_id_shelf_destination_id_destination_account"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "shelf_destination_id",
            "user_id",
            "kavita_chapter_id",
            name=op.f("pk_progress_sync_note"),
        ),
    )


def downgrade() -> None:
    op.drop_table("progress_sync_note")
