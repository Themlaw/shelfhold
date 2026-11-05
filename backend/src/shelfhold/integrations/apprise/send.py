"""One send through Apprise (EF-INT-006). The only module that calls `Apprise.notify`.

The URL carries the token: it is never logged, never put in an exception message and never
returned; the result is a code. Apprise's own logger is silenced for the same reason, and
redirects are off on the service (see `destination`).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final

from apprise import Apprise, NotifyFormat

from shelfhold.integrations.apprise.destination import ASSET, ChannelUrlRefused, _parse
from shelfhold.integrations.apprise.render import Message

CONNECT_TIMEOUT_S: Final = 8.0
READ_TIMEOUT_S: Final = 15.0

_apprise_logger = logging.getLogger("apprise")
_apprise_logger.setLevel(logging.CRITICAL)
_apprise_logger.addHandler(logging.NullHandler())
_apprise_logger.propagate = False  # a URL with a token must reach no handler of ours


@dataclass(frozen=True)
class SendResult:
    ok: bool
    code: str  # `notify.sent` or `notify.failed`: never a message from the service


def send_with_apprise(url: str, message: Message) -> SendResult:
    try:
        _scheme, service = _parse(url)
    except ChannelUrlRefused:
        return SendResult(ok=False, code="notify.failed")
    service.socket_connect_timeout = CONNECT_TIMEOUT_S
    service.socket_read_timeout = READ_TIMEOUT_S
    notifier = Apprise(asset=ASSET)
    notifier.add(service)
    try:
        result = notifier.notify(
            title=message.title, body=message.body, body_format=NotifyFormat.MARKDOWN
        )
    except Exception:  # a plugin may raise anything; the caller only needs to know it failed
        return SendResult(ok=False, code="notify.failed")
    sent = result.success_count >= 1 and result.failed_count == 0
    return SendResult(ok=sent, code="notify.sent" if sent else "notify.failed")
