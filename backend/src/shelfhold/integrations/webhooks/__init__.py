"""Webhooks (M4 L24, EF-INT-005): a persisted outbox, signed deliveries through the Fetcher.

What may be sent is the closed list of `catalogue`; to whom, `subscriptions` (the address through
the SSRF guard, the secret sealed); what is waiting, `outbox` (rows, a cursor that moves with them);
how it leaves, `deliver` (the Fetcher, the signature, the retries). Events are read only through
`events.integration_feed`, that is through `activity_view`: an import contract holds that line.
"""

from shelfhold.integrations.webhooks.admin import WebhookAdmin
from shelfhold.integrations.webhooks.catalogue import (
    MAX_WINDOW,
    WEBHOOK_EVENTS,
    Catalogue,
    CatalogueError,
    Deprecation,
    EventSpec,
    clean_payload,
    pick,
)
from shelfhold.integrations.webhooks.deliver import (
    ROTATION_WINDOW,
    SIGNATURE_HEADER,
    Deliverer,
    DeliveryReport,
    sign_body,
)
from shelfhold.integrations.webhooks.describe import webhook_documentation
from shelfhold.integrations.webhooks.outbox import (
    BASE_DELAY,
    LEASE,
    MAX_ATTEMPTS,
    MAX_DELAY,
    DeadDeliveryPublisher,
    Outbox,
    retry_delay,
)
from shelfhold.integrations.webhooks.subscriptions import (
    Opened,
    Subscription,
    SubscriptionStore,
    SubscriptionUnreadable,
    Target,
    WebhookRefused,
    check_target,
)

__all__ = [
    "BASE_DELAY",
    "LEASE",
    "MAX_ATTEMPTS",
    "MAX_DELAY",
    "MAX_WINDOW",
    "ROTATION_WINDOW",
    "SIGNATURE_HEADER",
    "WEBHOOK_EVENTS",
    "Catalogue",
    "CatalogueError",
    "DeadDeliveryPublisher",
    "Deliverer",
    "DeliveryReport",
    "Deprecation",
    "EventSpec",
    "Opened",
    "Outbox",
    "Subscription",
    "SubscriptionStore",
    "SubscriptionUnreadable",
    "Target",
    "WebhookAdmin",
    "WebhookRefused",
    "check_target",
    "clean_payload",
    "pick",
    "retry_delay",
    "sign_body",
    "webhook_documentation",
]
