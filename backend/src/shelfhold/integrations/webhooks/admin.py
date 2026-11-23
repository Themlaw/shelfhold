"""What the administrator does to webhooks, with its audit line in the same transaction.

The routes call this; it is the one place that joins the address check, the sealed row and the
audit (an action with no line, or a line for an action that did not happen, is not possible). The
audit names the subscription by its id: never the address, never the secret.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from datetime import datetime

from sqlalchemy import Connection, Engine

from shelfhold.identity.public import AuditEntry, Principal, audit_actor, audit_insert
from shelfhold.infrastructure.db import read_transaction, write_transaction
from shelfhold.infrastructure.fetcher import FetchError, Resolver
from shelfhold.infrastructure.secrets import SecretVault
from shelfhold.integrations.webhooks.outbox import RECENT, Delivery, recent
from shelfhold.integrations.webhooks.subscriptions import (
    Subscription,
    SubscriptionStore,
    WebhookRefused,
    check_target,
)

__all__ = ["WebhookAdmin"]


class WebhookAdmin:
    def __init__(
        self,
        engine: Engine,
        vault: Callable[[], SecretVault],
        resolver: Callable[[], Resolver],
        now: Callable[[], datetime],
    ) -> None:
        self._engine = engine
        self._vault = vault
        self._resolver = resolver
        self._now = now

    def create(
        self, principal: Principal, *, url: str, event_types: Iterable[str], allow_private: bool
    ) -> tuple[Subscription, str]:
        """Register a webhook. Raises `WebhookRefused` (the address, the types) or a Fetcher error
        (where the address leads): nothing is written then, and no audit line."""
        if principal.user_id is None:
            raise WebhookRefused("no_profile")  # a webhook is read through a profile's view
        try:
            target = asyncio.run(
                check_target(url, allow_private=allow_private, resolver=self._resolver())
            )
        except FetchError:  # where it leads: never which address, never what it resolved to
            raise WebhookRefused("address_refused") from None
        now = self._now()
        store = SubscriptionStore(self._vault())
        with write_transaction(self._engine) as connection:
            created, secret = store.create(
                connection, created_by=principal.user_id, url=url, event_types=event_types,
                target=target, now=now,
            )  # fmt: skip
            self._audit(connection, principal, "webhook.subscribe", created.id, now)
        return created, secret

    def listing(self) -> list[Subscription]:
        with read_transaction(self._engine) as connection:
            return SubscriptionStore(self._vault()).listing(connection)

    def get(self, subscription_id: str) -> Subscription | None:
        with read_transaction(self._engine) as connection:
            return SubscriptionStore(self._vault()).get(connection, subscription_id)

    def deliveries(self, subscription_id: str, *, limit: int = RECENT) -> list[Delivery] | None:
        """The last deliveries of one subscription (M6 L23b), for the screen: the rows of the
        outbox, newest first, and None when the subscription is not there. The sealed document is
        not opened — an address that cannot be read still shows what was sent to it."""
        with read_transaction(self._engine) as connection:
            return recent(connection, subscription_id, limit=limit)

    def delete(self, principal: Principal, subscription_id: str) -> bool:
        now = self._now()
        with write_transaction(self._engine) as connection:
            if not SubscriptionStore(self._vault()).delete(connection, subscription_id):
                return False
            self._audit(connection, principal, "webhook.unsubscribe", subscription_id, now)
        return True

    def rotate(self, principal: Principal, subscription_id: str) -> tuple[Subscription, str] | None:
        """Turn the signing secret (M6 L23a): the previous one keeps signing for 24 h, the new one
        is returned here and nowhere again. None when the subscription is not there; a sealed
        document that cannot be opened is not rotated (`SubscriptionUnreadable`)."""
        now = self._now()
        with write_transaction(self._engine) as connection:
            turned = SubscriptionStore(self._vault()).rotate(
                connection, subscription_id, now=now
            )  # fmt: skip
            if turned is None:
                return None
            self._audit(connection, principal, "webhook.rotate", subscription_id, now)
            return turned

    @staticmethod
    def _audit(
        connection: Connection,
        principal: Principal,
        action: str,
        subscription_id: str,
        now: datetime,
    ) -> None:
        connection.execute(
            audit_insert(
                AuditEntry(
                    actor_id=audit_actor(principal),
                    action=action,
                    at=now,
                    target=f"webhook:{subscription_id}",
                    session_id=principal.session_id,
                    auth_time=principal.auth_time,
                )
            )
        )
