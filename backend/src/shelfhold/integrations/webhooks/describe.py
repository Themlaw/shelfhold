"""The `x-webhooks` part of the description of the API: what a receiver is sent, how to check it,
and the events (with the versions of their shape) a subscription may ask for."""

from __future__ import annotations

from typing import Any

from shelfhold.integrations.webhooks.catalogue import WEBHOOK_EVENTS
from shelfhold.integrations.webhooks.deliver import SIGNATURE_HEADER
from shelfhold.integrations.webhooks.outbox import MAX_ATTEMPTS

__all__ = ["webhook_documentation"]


def webhook_documentation() -> dict[str, Any]:
    events: dict[str, Any] = {}
    for spec in WEBHOOK_EVENTS.specs():
        events[spec.type] = {
            "current_version": spec.current,
            "fields": {
                str(version): list(getattr(shape, "fields", ()))
                for version, shape in sorted(spec.render.items())
            },
            "deprecated": {
                str(version): {
                    "since": window.since.isoformat(),
                    "sunset": window.sunset.isoformat(),
                }
                for version, window in spec.deprecated.items()
            },
        }
    return {
        "delivery": {
            "method": "POST",
            "content_type": "application/json",
            "body": {
                "event_id": "integer, the number of the event",
                "type": "string, one of `events`",
                "schema_version": "integer, the version of the shape of `data`",
                "occurred_at": "string, an ISO 8601 instant (UTC)",
                "data": "object, identifiers, counts and codes only: never a title",
            },
            "headers": {
                SIGNATURE_HEADER: (
                    "t=<unix seconds>,v1=<hex HMAC-SHA256 of '<t>.' followed by the exact body, "
                    "keyed by the secret given once at registration or rotation>; for 24 h after "
                    "a rotation a second v1= made with the previous secret travels beside the "
                    "new one: accept the delivery when any one v1 matches, comparing in constant "
                    "time (the header is a list of key=value pairs, not a dictionary)"
                ),
                "X-Shelfhold-Event": "the event type",
                "X-Shelfhold-Delivery": "identity of the delivery, the same at every retry: drop "
                "a second copy",
                "X-Shelfhold-Schema-Version": "the version of the shape of `data`",
            },
            "retries": f"{MAX_ATTEMPTS} attempts, the delay doubling from 30 s up to 6 h; "
            "a 2xx answer settles it; a redirection is not followed",
            "deprecation": "while a shape is deprecated, an event is sent twice (the old version, "
            "then the current one), for at most 180 days",
        },
        "events": events,
    }
