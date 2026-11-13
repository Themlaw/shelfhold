"""Per-person notifications: routing rules, router, sender, and the way out (EF-INT-021)."""

from shelfhold.integrations.notify.delivery import ChannelDelivery
from shelfhold.integrations.notify.routing import Recipient, classify, delivery_time
from shelfhold.integrations.notify.service import (
    MAX_ATTEMPTS,
    NotificationRouter,
    NotificationSender,
    SendReport,
)

__all__ = [
    "MAX_ATTEMPTS",
    "ChannelDelivery",
    "NotificationRouter",
    "NotificationSender",
    "Recipient",
    "SendReport",
    "classify",
    "delivery_time",
]
