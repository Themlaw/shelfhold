"""The configured channels: names in clear, URLs sealed (ENF-SEC-005, ENF-SEC-016).

An Apprise URL carries a token. All of them are one JSON document sealed with `SecretVault` in
the row `core.notifications` of `source_config` (a table that already has the sealed columns and
is rotated with the others; no migration was granted to this lot). The clear `settings` of that
row holds only the channel names, which the bus reads without the vault. Opening the document
registers every URL with `register_secret_value`, so the log filter scrubs them by value.
"""

from __future__ import annotations

import json
import re
from typing import Final

import sqlalchemy as sa
from sqlalchemy import Connection, Engine

from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.schema import acquisition
from shelfhold.infrastructure.secrets.vault import SecretVault
from shelfhold.integrations.apprise.destination import ValidatedChannel, register_url_secrets

ROW_ID: Final = "core.notifications"
_NAME: Final = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")
source_config = acquisition.source_config


class ChannelNames:
    """The `ChannelSource` of a bus that has no vault: only the names are read, in clear.

    Enough for a publisher (a CLI command, the server before its keyring exists) to queue one
    `notification_pending` row per channel; the URLs stay sealed and only the sender opens them.
    """

    def channel_names(self, connection: Connection) -> list[str]:
        settings = connection.execute(
            sa.select(source_config.c.settings).where(source_config.c.plugin_id == ROW_ID)
        ).scalar_one_or_none()
        return sorted((settings or {}).get("channels", []))


def operating_bus(clock: Clock) -> EventBus:
    """The bus of the application: an operating event queues one notification per channel (the
    sender, `notify.dispatch`, opens the sealed URLs later). Every publisher builds it here."""
    return EventBus(clock, channels=ChannelNames())


class ChannelStore:
    def __init__(self, vault: SecretVault, clock: Clock) -> None:
        self._vault = vault
        self._clock = clock

    # -- ChannelSource ------------------------------------------------------------------------

    def channel_names(self, connection: Connection) -> list[str]:
        settings = connection.execute(
            sa.select(source_config.c.settings).where(source_config.c.plugin_id == ROW_ID)
        ).scalar_one_or_none()
        return sorted((settings or {}).get("channels", []))

    def names(self, engine: Engine) -> list[str]:
        with engine.connect() as connection:
            return self.channel_names(connection)

    # -- secrets ------------------------------------------------------------------------------

    def urls(self, connection: Connection) -> dict[str, str]:
        sealed = connection.execute(
            sa.select(source_config.c.secret_kid).where(source_config.c.plugin_id == ROW_ID)
        ).scalar_one_or_none()
        if sealed is None:
            return {}
        document = json.loads(self._vault.open_row(connection, "source_config", ROW_ID))
        urls: dict[str, str] = dict(document.get("channels", {}))
        for url in urls.values():
            register_url_secrets(url)
        return urls

    def save(self, connection: Connection, name: str, channel: ValidatedChannel) -> None:
        if not _NAME.fullmatch(name):
            raise ValueError("a channel name is a short lowercase slug")
        urls = self.urls(connection)
        urls[name] = channel.url
        self._write(connection, urls)

    def remove(self, connection: Connection, name: str) -> bool:
        urls = self.urls(connection)
        if urls.pop(name, None) is None:
            return False
        self._write(connection, urls)
        return True

    def _write(self, connection: Connection, urls: dict[str, str]) -> None:
        now = self._clock.now()
        exists = connection.execute(
            sa.select(source_config.c.plugin_id).where(source_config.c.plugin_id == ROW_ID)
        ).first()
        settings = {"channels": sorted(urls)}
        if exists is None:
            connection.execute(sa.insert(source_config).values(plugin_id=ROW_ID, settings=settings))
        else:
            connection.execute(
                sa.update(source_config)
                .where(source_config.c.plugin_id == ROW_ID)
                .values(settings=settings, updated_at=now)
            )
        document = json.dumps({"channels": urls}, sort_keys=True).encode()
        self._vault.seal_row(connection, "source_config", ROW_ID, document)
