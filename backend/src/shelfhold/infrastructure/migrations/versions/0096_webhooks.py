"""The outbox and the subscriptions of the webhooks (M4 L24, EF-INT-005).

Two tables, `webhook_subscription` (the sealed address and secret) and `webhook_outbox` (what is
still to be sent, one row per subscription, event and version of its shape). Created from the
schema so the DDL stays identical to `metadata.create_all` (ENF-OPS-002). Nothing existing is
touched; reversible.

Number: 0096 follows 0095 (L26), the head of main when this lot was frozen.
"""

from alembic import op

from shelfhold.infrastructure.schema import webhooks

revision = "0096"
down_revision = "0095"
branch_labels = None
depends_on = None


def upgrade() -> None:
    webhooks.webhook_subscription.create(op.get_bind(), checkfirst=True)
    webhooks.webhook_outbox.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    webhooks.webhook_outbox.drop(op.get_bind(), checkfirst=True)
    webhooks.webhook_subscription.drop(op.get_bind(), checkfirst=True)
