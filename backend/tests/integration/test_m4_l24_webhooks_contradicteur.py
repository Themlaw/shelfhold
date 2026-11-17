"""PR #335 (M4 L24), relecture adverse: the holes found, each as a red proof (strict xfail).

When a fix lands, its test turns green, the strict xfail fails, and the marker is removed.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from shelfhold.composition import Network
from shelfhold.config import Settings
from tests.auth_env import ADMIN, CSRF_COOKIE, CSRF_HEADER, AuthEnv, auth_env
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.webhooks_env import webhook_env


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    resolver = FakeResolver({"hooks.example.org": ["93.184.216.34"]})
    network = Network(resolver=resolver, transport=Recorder().transport)
    with auth_env(monkeypatch, settings, network=network) as env:
        assert env.login(ADMIN).status_code == 200
        yield env


@pytest.mark.req("EF-INT-005", "ENF-SEC-005")
def test_the_creation_answer_that_carries_the_secret_is_not_stored(api: AuthEnv) -> None:
    made = api.client.post(
        "/api/v1/webhooks",
        json={"url": "https://hooks.example.org/in/one", "event_types": ["unit.stored"]},
        headers={CSRF_HEADER: api.client.cookies.get(CSRF_COOKIE) or ""},
    )
    assert made.status_code == 201 and made.json()["secret"]
    assert "no-store" in made.headers.get("cache-control", "").lower()


@pytest.mark.req("EF-INT-005")
def test_a_backlog_on_one_address_does_not_starve_another(settings: Settings) -> None:
    with webhook_env(settings) as env:
        noisy, _ = env.subscribe(types=("unit.stored",))
        quiet, _ = env.subscribe(types=("disk.low",))
        for count in range(1, 201):
            env.publish("unit.stored", {"count": count})
        env.publish("disk.low", {"count": 1})
        outbox = env.outbox()
        outbox.enqueue()
        claimed = outbox.claim(20)
        assert sum(r.subscription_id == noisy.id for r in claimed) <= 5
        assert any(r.subscription_id == quiet.id for r in claimed)
