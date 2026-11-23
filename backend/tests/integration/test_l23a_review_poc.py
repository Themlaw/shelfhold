"""Relecture adverse de M6 L23a : ce que la rotation et l'événement `webhook.dead` ne tiennent pas.

`webhook.dead` est dans le catalogue des webhooks : un abonnement peut le demander. Une livraison
abandonnée vers un récepteur en panne écrit alors un `webhook.dead`, que l'outbox met en file pour
ce même récepteur, qui l'abandonne à son tour, et ainsi de suite : une boucle sans fin, qui double
à chaque tour dès que deux abonnements le demandent. Hôtes synthétiques, secrets faits au test.
"""

from __future__ import annotations

import httpx2
import pytest

from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.integrations.webhooks import MAX_ATTEMPTS
from tests.webhooks_env import webhook_env


@pytest.mark.req("EF-INT-005")
def test_a_dead_delivery_of_webhook_dead_does_not_feed_itself(settings: Settings) -> None:
    with webhook_env(settings, respond=lambda _r: httpx2.Response(503)) as env:
        types = ("unit.stored", "webhook.dead")
        first, _ = env.subscribe(types=types)
        second, _ = env.subscribe(url="https://hooks.example.org/in/two", types=types)
        env.publish()  # one event, two subscriptions, one receiver down
        bus = EventBus(env.clock)
        for _ in range(3 * MAX_ATTEMPTS + 2):  # three lives of a row, end to end
            env.deliverer(publisher=bus).run_once()
            env.clock.advance(hours=7)  # past the longest retry delay (6 h)
        dead = env.events("webhook.dead")
        # one abandoned delivery per subscription of the one event: two announcements, no more
        assert len(dead) == 2, (
            f"{len(dead)} webhook.dead for one event: each dead delivery of webhook.dead "
            "makes another"
        )
