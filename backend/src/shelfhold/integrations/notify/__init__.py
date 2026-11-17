"""Per-person notifications: routing rules, router, sender, the way out, and the destination of
one adult profile (EF-INT-021)."""

from shelfhold.integrations.notify.delivery import ChannelDelivery
from shelfhold.integrations.notify.profile_destination import (
    ProfileDelivery,
    ProfileDestinationError,
    ProfileDestinations,
)
from shelfhold.integrations.notify.routing import (
    Recipient,
    classify,
    delivery_time,
    is_immediate,
)
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
    "ProfileDelivery",
    "ProfileDestinationError",
    "ProfileDestinations",
    "Recipient",
    "SendReport",
    "classify",
    "delivery_time",
    "is_immediate",
]
