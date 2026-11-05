"""Apprise, behind its adapter: the operating notifications (EF-INT-006, ENF-SEC-016).

Apprise is imported only here (import-linter). It makes its own HTTP requests, outside the
Fetcher: see `destination` for what is enforced instead and for the documented limit.
"""

from shelfhold.integrations.apprise.channels import ChannelStore
from shelfhold.integrations.apprise.destination import (
    ChannelUrlRefused,
    ValidatedChannel,
    validate_channel_url,
)
from shelfhold.integrations.apprise.dispatch import (
    MAX_ATTEMPTS,
    DispatchReport,
    NotificationDispatcher,
)
from shelfhold.integrations.apprise.jobs import register_notification_job, request_dispatch
from shelfhold.integrations.apprise.render import Message, neutralize_markdown, render_notification
from shelfhold.integrations.apprise.schemes import ALLOWED_SCHEMES
from shelfhold.integrations.apprise.send import SendResult, send_with_apprise

__all__ = [
    "ALLOWED_SCHEMES",
    "MAX_ATTEMPTS",
    "ChannelStore",
    "ChannelUrlRefused",
    "DispatchReport",
    "Message",
    "NotificationDispatcher",
    "SendResult",
    "ValidatedChannel",
    "neutralize_markdown",
    "register_notification_job",
    "render_notification",
    "request_dispatch",
    "send_with_apprise",
    "validate_channel_url",
]
