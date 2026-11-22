"""Sending: sign the body, hand it to the Fetcher, settle the row.

A webhook leaves only through the Fetcher and its SSRF guard, which resolves the name again at
**every** delivery and connects only to an address it validated (a name that was public at
registration and private now is refused, and the row says `ssrf.blocked`). Redirections are never
followed: a 3xx is a failed attempt. A private target the administrator allowed is reachable as that
exact `host:port` with the addresses it had then, and nothing else.

The body is signed as `HMAC-SHA256(secret, "<t>." + body)` and sent as
`X-Shelfhold-Signature: t=<unix seconds>,v1=<hex>`; the receiver rejects a stale `t`. For 24 h
after a rotation, a second `v1=` made with the previous secret travels beside the new one, so a
receiver that moves over loses no event (M6 L23a); the first delivery that sees the window closed
drops that secret from the sealed document. The secret is
opened for the call and never logged. A failure is a closed code and a count in the log, never the
address or the body. A delivery that is given up is settled as dead and announced once by the
`webhook.dead` event, written in the same transaction — unless the delivery was itself of a
`webhook.dead`, which announces nothing (M6 L23a review).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import structlog
from sqlalchemy import Engine

from shelfhold.events.integration_feed import gone_reason, read_one
from shelfhold.events.upcast import Upcaster
from shelfhold.infrastructure.clock import Clock
from shelfhold.infrastructure.db import read_transaction
from shelfhold.infrastructure.fetcher import (
    FetchError,
    FetchLimits,
    GuardedFetcher,
    IntegrationFetcher,
    LanTargets,
    Resolver,
)
from shelfhold.infrastructure.fetcher.lan import normalize_host
from shelfhold.infrastructure.secrets import SecretVault
from shelfhold.integrations.webhooks.catalogue import WEBHOOK_EVENTS, Catalogue
from shelfhold.integrations.webhooks.outbox import (
    BATCH,
    LEASE,
    MAX_ATTEMPTS,
    DeadDeliveryPublisher,
    Due,
    Outbox,
)
from shelfhold.integrations.webhooks.subscriptions import (
    ROTATION_WINDOW,
    Opened,
    SubscriptionStore,
    SubscriptionUnreadable,
    is_loopback,
)

__all__ = [
    "MAX_ATTEMPTS",
    "ROTATION_WINDOW",
    "SIGNATURE_HEADER",
    "Deliverer",
    "DeliveryReport",
    "sign_body",
]

log = structlog.get_logger(__name__)

SIGNATURE_HEADER: Final = "X-Shelfhold-Signature"
# The rotation window itself lives in `subscriptions`: the store needs it to drop the previous
# secret from the sealed document once it has expired (M6 L23a review).
LIMITS: Final = FetchLimits(
    max_bytes=64 * 1024, total_timeout=20.0, connect_timeout=5.0, read_timeout=10.0,
    max_redirects=0,  # a redirection is never followed
)  # fmt: skip


assert BATCH * 20.0 < LEASE  # the lease outlasts the whole batch, at the longest send  # noqa: S101


def sign_body(secret: str, timestamp: int, body: bytes) -> str:
    """The `v1` value: HMAC-SHA256 of `"<timestamp>."` and the exact bytes sent, in hex."""
    return hmac.new(secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256).hexdigest()


@dataclass
class DeliveryReport:
    delivered: int = 0
    retried: int = 0  # failed, due again later
    refused: int = 0  # the guard refused the address (also counted as retried or dead)
    dead: int = 0


class Deliverer:
    def __init__(
        self,
        engine: Engine,
        vault: SecretVault,
        clock: Clock,
        resolver: Resolver,
        *,
        transport: Any = None,  # a test's mock transport; None: the real one
        catalogue: Catalogue = WEBHOOK_EVENTS,
        steps: Mapping[tuple[str, int], Upcaster] | None = None,
        batch: int = BATCH,
        publisher: DeadDeliveryPublisher | None = None,
    ) -> None:
        self._engine = engine
        self._clock = clock
        self._resolver = resolver
        self._transport = transport
        self._catalogue = catalogue
        self._steps = steps
        self._batch = batch
        self._store = SubscriptionStore(vault, catalogue=catalogue)
        self._outbox = Outbox(engine, clock, catalogue=catalogue, publisher=publisher)

    def run_once(self) -> DeliveryReport:
        """Make the rows of the new events, then send what is due."""
        self._outbox.enqueue()
        report = DeliveryReport()
        due = self._outbox.claim(self._batch)
        if due:
            asyncio.run(self._send_all(due, report))
        return report

    async def _send_all(self, due: list[Due], report: DeliveryReport) -> None:
        for row in due:
            await self._send(row, report)

    async def _send(self, row: Due, report: DeliveryReport) -> None:
        try:
            with read_transaction(self._engine) as connection:
                opened = self._store.open(connection, row.subscription_id)
                event = read_one(
                    connection, row.event_id, as_user=opened.subscription.created_by,
                    steps=self._steps, require_admin=True,
                )  # fmt: skip
                # why nothing would be left to send, asked while the connection is open (M6 L23a
                # review): the profile that made the subscription, or the event itself
                gone = (
                    None
                    if event is not None
                    else gone_reason(connection, as_user=opened.subscription.created_by)
                )
        except SubscriptionUnreadable:  # the sealed document: each try counts, up to the ceiling
            log.warning("webhook.subscription_unreadable")
            self._retry(row, report, status=None, error="subscription_unreadable")
            return
        except Exception:  # the database: ours, not the receiver's
            log.warning("webhook.subscription_unreadable")
            self._outbox.release(row)  # the attempt is not counted: a long outage kills nothing
            return
        self._drop_expired_previous(opened, row.subscription_id)
        data = (
            None if event is None
            else self._catalogue.render(event.type, row.schema_version, event.payload)
        )  # fmt: skip
        if (
            event is None or data is None
        ):  # purged, no longer theirs, or no longer exposed: nothing to wait for
            self._outbox.failed(
                row, status=None, error=gone or "event_gone", final=True, reason=gone
            )  # fmt: skip
            report.dead += 1
            return
        body = json.dumps(
            {"event_id": event.event_id, "type": event.type, "schema_version": row.schema_version,
             "occurred_at": event.occurred_at.isoformat(), "data": data},
            separators=(",", ":"), sort_keys=True,
        ).encode()  # fmt: skip
        stamp = int(self._clock.now().timestamp())
        signatures = self._signatures(opened, stamp, body)
        headers = {
            "Content-Type": "application/json",
            SIGNATURE_HEADER: f"t={stamp}," + ",".join(f"v1={digest}" for digest in signatures),
            "X-Shelfhold-Event": event.type,
            "X-Shelfhold-Delivery": row.id,  # stable across attempts: the receiver's dedupe key
            "X-Shelfhold-Schema-Version": str(row.schema_version),
        }
        if any(is_loopback(ipaddress.ip_address(a)) for a in opened.subscription.private_addresses):
            report.refused += 1  # written before the rule that keeps the loopback out
            self._retry(row, report, status=None, error="ssrf.blocked")
            return
        fetcher = self._fetcher(opened)
        try:
            response = await fetcher.post(opened.url, content=body, headers=headers)
        except FetchError as exc:
            refused = exc.code in ("ssrf.blocked", "source.dns_blocked", "fetch.dns_failed")
            report.refused += int(refused)
            self._retry(row, report, status=None, error=exc.code)
            return
        except Exception:  # a transport that failed in a way nobody named: a count, no message
            self._retry(row, report, status=None, error="delivery_error")
            return
        finally:
            await fetcher.aclose()
        if 200 <= response.status < 300:
            self._outbox.delivered(row, response.status)
            report.delivered += 1
        else:
            self._retry(row, report, status=response.status, error="http_status")

    def _drop_expired_previous(self, opened: Opened, subscription_id: str) -> None:
        """The previous secret leaves the sealed document at the first delivery that sees its
        window closed (M6 L23a review): it signs nothing any more, and the document stops
        carrying it."""
        rotated_at = opened.subscription.rotated_at
        if opened.secret_prev is None or rotated_at is None:
            return
        if self._clock.now() < rotated_at + ROTATION_WINDOW:
            return
        try:
            self._store.drop_expired_previous(self._engine, subscription_id, now=self._clock.now())
        except SubscriptionUnreadable:  # it will be counted at the next opening, not here
            log.warning("webhook.subscription_unreadable")

    def _signatures(self, opened: Opened, stamp: int, body: bytes) -> list[str]:
        """The `v1` values of this delivery: the current secret, and the previous one while the
        rotation window is open (M6 L23a). After the window the old secret signs nothing."""
        signed = [sign_body(opened.secret, stamp, body)]
        previous, rotated_at = opened.secret_prev, opened.subscription.rotated_at
        if (
            previous is not None
            and rotated_at is not None
            and self._clock.now() < rotated_at + ROTATION_WINDOW
        ):
            signed.append(sign_body(previous, stamp, body))
        return signed

    def _retry(self, row: Due, report: DeliveryReport, *, status: int | None, error: str) -> None:
        if self._outbox.failed(row, status=status, error=error):
            report.dead += 1
        else:
            report.retried += 1
        log.warning("webhook.delivery_failed", error=error, status=status, attempt=row.attempts)

    def _fetcher(self, opened: Opened) -> GuardedFetcher:
        """The Fetcher of this delivery: the default guard, or, for the one private target the
        administrator allowed, that exact host and port with the addresses it had."""
        subscription = opened.subscription
        if subscription.private_target is None:
            return GuardedFetcher(self._resolver, limits=LIMITS, transport=self._transport)
        host, _, port = subscription.private_target.rpartition(":")
        pinned = frozenset(ipaddress.ip_address(a) for a in subscription.private_addresses)
        lan = LanTargets({(normalize_host(host.strip("[]")), int(port)): pinned})
        return IntegrationFetcher(
            self._resolver, lan_targets=lan, limits=LIMITS, transport=self._transport
        )
