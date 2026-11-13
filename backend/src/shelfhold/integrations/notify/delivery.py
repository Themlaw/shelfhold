"""The way out of a per-person message: the channels the admin configured (EF-INT-021, ENF-SEC-016).

No profile has a destination of its own in the schema, so a message leaves through the instance's
channels (the same sealed URLs, opened at each send, checked again against the allowlist and the
SSRF rules of `integrations.apprise.destination`). Several people may read those channels, which is
why the sender is built with `private_titles=False` in production. No URL, no title and no text of
an error reaches a log line: a failure is a count and a code.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import structlog
from sqlalchemy import Engine

from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.fetcher import LanTargets, Resolver, SystemResolver
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.integrations.apprise.channels import ChannelStore
from shelfhold.integrations.apprise.destination import validate_channel_url
from shelfhold.integrations.apprise.render import Message
from shelfhold.integrations.apprise.send import SendResult, send_with_apprise

log = structlog.get_logger(__name__)

Send = Callable[[str, Message], SendResult]


class ChannelDelivery:
    def __init__(
        self,
        engine: Engine,
        vault: SecretVault,
        clock: Clock,
        *,
        send: Send = send_with_apprise,
        lan_targets: Callable[[], LanTargets] = LanTargets.empty,
        resolver: Resolver | None = None,
    ) -> None:
        self._engine = engine
        self._store = ChannelStore(vault, clock)
        self._send = send
        self._lan_targets = lan_targets
        self._resolver = resolver or SystemResolver()

    def __call__(self, user_id: str, message: Message) -> bool:
        try:
            with self._engine.connect() as connection:
                urls = self._store.urls(connection)
        except Exception:  # undecryptable: the row stays and the sweep tries again
            log.warning("notify.channels_unreadable")
            return False
        if not urls:
            return True  # no channel is configured: nothing to wait for, nothing to pile up
        delivered = False
        for url in urls.values():
            try:
                asyncio.run(
                    validate_channel_url(url, lan=self._lan_targets(), resolver=self._resolver)
                )
                delivered = self._send(url, message).ok or delivered
            except Exception:
                log.warning("notify.digest_send_failed")  # never the URL or the text
        return delivered
