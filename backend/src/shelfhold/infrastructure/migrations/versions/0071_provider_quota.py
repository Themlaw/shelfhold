"""What the metadata providers keep in the database (M4 L20): the announced rate, the calendar.

- `provider_quota.announced_limit`: the rate (per minute) a provider last announced in a response
  header, shared by every worker. Every old row stays, with nothing announced;
- `provider_release_calendar`: the release calendar of a followed series, cached so that
  `series.stale` reads it without any request.
"""

import sqlalchemy as sa
from alembic import op

revision = "0071"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("provider_quota") as batch:
        batch.add_column(sa.Column("announced_limit", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            op.f("ck_provider_quota_limit_positive"),
            '"announced_limit" IS NULL OR "announced_limit" > 0',
        )
    op.create_table(
        "provider_release_calendar",
        sa.Column("provider", sa.String(), nullable=False),
        sa.Column("edition_id", sa.String(length=26), nullable=False),
        sa.Column("remote_id", sa.String(), nullable=False),
        sa.Column("last_release_on", sa.Date(), nullable=True),
        sa.Column("chapter", sa.String(), nullable=True),
        sa.Column("volume", sa.String(), nullable=True),
        sa.Column("source_url", sa.String(), nullable=False),
        sa.Column("attribution", sa.String(), nullable=False),
        sa.Column("fetched_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "\"provider\" <> '' AND \"remote_id\" <> ''",
            name=op.f("ck_provider_release_calendar_provider_remote_named"),
        ),
        sa.ForeignKeyConstraint(
            ["edition_id"],
            ["edition.id"],
            name=op.f("fk_provider_release_calendar_edition_id_edition"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "provider", "edition_id", name=op.f("pk_provider_release_calendar")
        ),
    )


def downgrade() -> None:
    op.drop_table("provider_release_calendar")
    with op.batch_alter_table("provider_quota") as batch:
        batch.drop_constraint(op.f("ck_provider_quota_limit_positive"), type_="check")
        batch.drop_column("announced_limit")
