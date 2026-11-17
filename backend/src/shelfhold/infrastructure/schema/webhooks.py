"""Webhooks (M4 L24, EF-INT-005): the subscriptions an administrator registered and the outbox of
what is still to be sent to them.

A subscription holds its address and its signing secret in one sealed document (the address of a
webhook often is a secret: a token in its path), and shows only the host. The outbox is rows: an
event, a subscription and the version of the event's shape to send; the same three can only exist
once, so a sweep that runs twice, or again after a crash, makes nothing twice.
"""

import sqlalchemy as sa

from shelfhold.infrastructure.schema._base import (
    UtcDateTime,
    bool_column,
    in_check,
    json_shape_check,
    metadata,
    now_column,
    positive_check,
    ulid_check,
    ulid_column,
)
from shelfhold.infrastructure.schema.acquisition import sealed_secret_columns

OUTBOX_STATUSES = ("pending", "delivered", "dead")

webhook_subscription = sa.Table(
    "webhook_subscription",
    metadata,
    ulid_column("id", primary_key=True),  # a sealed table has a single-column key (rotation)
    # The profile whose view the payload is read through; gone with the profile: nothing is sent
    # to a subscription nobody answers for (the scope of nobody sees nothing).
    sa.Column("created_by", sa.String(26), sa.ForeignKey("user.id", ondelete="SET NULL")),
    sa.Column("url_host", sa.String(), nullable=False),  # what is shown: never the path or query
    sa.Column("event_types", sa.JSON(), nullable=False),
    # `host:port` the administrator allowed to be a private address, and the addresses it had
    # when she did: a record that later moves is refused (`LanTargets` semantics).
    sa.Column("private_target", sa.String(), nullable=True),
    sa.Column("private_addresses", sa.JSON(none_as_null=True), nullable=True),
    bool_column("enabled", default=True),
    # The last event this subscription has been given the chance to take, moved in the same
    # transaction as the rows it made.
    sa.Column("last_event_id", sa.Integer(), nullable=False),
    *sealed_secret_columns(),
    now_column(),
    ulid_check("id"),
    positive_check("last_event_id"),
    json_shape_check("event_types", "array"),
    sa.CheckConstraint(
        '("private_target" IS NULL) = ("private_addresses" IS NULL)', name="private_pinned"
    ),
    sa.CheckConstraint("\"url_host\" <> ''", name="host_named"),
)

webhook_outbox = sa.Table(
    "webhook_outbox",
    metadata,
    ulid_column("id", primary_key=True),  # the delivery identity: the receiver's idempotency key
    sa.Column(
        "subscription_id",
        sa.String(26),
        sa.ForeignKey("webhook_subscription.id", ondelete="CASCADE"),
        nullable=False,
    ),
    sa.Column(
        "event_id", sa.Integer(), sa.ForeignKey("event.id", ondelete="CASCADE"), nullable=False
    ),
    sa.Column("schema_version", sa.Integer(), nullable=False),
    sa.Column("status", sa.String(), nullable=False, server_default="pending"),
    sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    sa.Column("next_try_at", UtcDateTime(), nullable=False),
    sa.Column("last_status", sa.Integer(), nullable=True),  # an HTTP status, or nothing
    sa.Column("last_error", sa.String(), nullable=True),  # a code, never a message or an address
    sa.Column("delivered_at", UtcDateTime(), nullable=True),
    now_column(),
    ulid_check("id"),
    in_check("status", OUTBOX_STATUSES),
    positive_check("attempts"),
    positive_check("schema_version", minimum=1),
    sa.UniqueConstraint("subscription_id", "event_id", "schema_version"),
    sa.Index("ix_webhook_outbox_due", "status", "next_try_at"),
)
