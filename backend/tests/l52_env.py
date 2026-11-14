"""L52: what the operating-event tests share: a notification channel on a real instance, and a
look at the `event` and `notification_pending` rows the production bus wrote."""

from __future__ import annotations

import asyncio
from typing import Any

import sqlalchemy as sa
from sqlalchemy import Engine

from shelfhold.composition import Composition
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.fetcher import LanTargets
from shelfhold.infrastructure.schema import platform
from shelfhold.integrations.apprise import ChannelStore, validate_channel_url
from tests.fakes_fetcher import FakeResolver
from tests.notifications_env import CHANNEL_URL, PUBLIC_IP


def add_channel(comp: Composition, name: str = "ops") -> None:
    """A real, sealed channel: the bus of the application then queues one row for it."""
    resolver = FakeResolver({"ntfy.example.org": [PUBLIC_IP]})
    checked = asyncio.run(
        validate_channel_url(CHANNEL_URL, lan=LanTargets.empty(), resolver=resolver)
    )
    with write_transaction(comp.engine) as connection:
        ChannelStore(comp.vault(), comp.clock).save(connection, name, checked)


def events_of(engine: Engine, event_type: str) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(platform.event.c.payload)
            .where(platform.event.c.type == event_type)
            .order_by(platform.event.c.id)
        )
        return [dict(r.payload) for r in rows]


def notified(engine: Engine, event_type: str) -> list[str]:
    """The channels a notification is waiting on for events of this type."""
    event, pending = platform.event, platform.notification_pending
    with engine.connect() as connection:
        rows = connection.execute(
            sa.select(pending.c.channel)
            .join(event, event.c.id == pending.c.event_id)
            .where(event.c.type == event_type)
            .order_by(pending.c.id)
        )
        return [str(r.channel) for r in rows]


def name_channel(engine: Engine, name: str = "ops") -> None:
    """A channel by its name only: all the bus reads to queue a notification (the sealed URL is
    the sender's business)."""
    from shelfhold.infrastructure.schema import acquisition
    from shelfhold.integrations.apprise.channels import ROW_ID

    with write_transaction(engine) as connection:
        connection.execute(
            sa.insert(acquisition.source_config).values(
                plugin_id=ROW_ID, settings={"channels": [name]}
            )
        )
