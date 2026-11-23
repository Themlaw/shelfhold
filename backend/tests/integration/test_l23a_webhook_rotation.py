"""M6 L23a (EF-INT-005, D23): the rotation of a webhook's secret, and the dead delivery event.

Rotating is the administrator's alone, a reserved action (`webhook.manage`): a fresh session,
never a key. The new secret is shown once, in the answer to the rotation and nowhere else. For
24 h after the rotation, each body carries two `v1=` signatures (the previous secret and the new
one), so a receiver that moves over loses no event; after the window the old secret signs
nothing. A delivery that is given up emits `webhook.dead` once, in the same transaction as the
row that dies — unless the delivery was itself of a `webhook.dead`, which announces nothing (the
announcement would feed the loop it reports). The previous secret leaves the sealed document at
the first delivery that sees its window closed, and a creator who is no longer an administrator
is named by a reason of their own (`not_theirs`). Synthetic hosts and addresses; secrets made at
run time.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

import httpx2
import pytest
import sqlalchemy as sa

from shelfhold.composition import Network
from shelfhold.config import Settings
from shelfhold.events.bus import EventBus
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.integrations.webhooks import (
    MAX_ATTEMPTS,
    Catalogue,
    Deliverer,
    EventSpec,
    pick,
    sign_body,
)
from shelfhold.integrations.webhooks.outbox import Due
from tests.auth_env import ADMIN, AuthEnv, auth_env, db_rows
from tests.fakes_fetcher import FakeResolver, Recorder
from tests.integration.test_m4_l24_webhook_routes import (
    BASE,
    CREATE,
    _bearer,
    _key,
    _refused,
    _send,
)
from tests.webhooks_env import HOOK, webhook_env


def _signatures(request: httpx2.Request) -> tuple[str, list[str]]:
    """The stamp and every `v1` of the signature header, in order (the new secret first)."""
    parts = [piece.split("=", 1) for piece in request.headers["x-shelfhold-signature"].split(",")]
    assert parts[0][0] == "t", request.headers["x-shelfhold-signature"]
    assert [key for key, _ in parts] == ["t", *["v1"] * (len(parts) - 1)]
    return parts[0][1], [value for _, value in parts[1:]]


# -- the two-signature window -------------------------------------------------------------------


@pytest.mark.req("EF-INT-005")
def test_webhook_rotation_signs_twice_during_window(settings: Settings) -> None:
    with webhook_env(settings) as env:
        sub, first = env.subscribe()
        with write_transaction(env.engine) as connection:
            _, second = env.store.rotate(connection, sub.id, now=env.clock.now())
        assert second != first and second.startswith("whsec_")

        env.publish()
        env.outbox().enqueue()
        assert env.deliverer().run_once().delivered == 1
        [request] = env.recorder.requests
        stamp, signatures = _signatures(request)
        assert len(signatures) == 2  # the old secret and the new one, for the receiver that moves
        assert set(signatures) == {
            sign_body(first, int(stamp), request.content),
            sign_body(second, int(stamp), request.content),
        }

        # the window is 24 h: at 23 h the previous secret still signs
        env.publish("unit.stored", {"count": 5})
        env.outbox().enqueue()
        env.clock.advance(hours=23)
        assert env.deliverer().run_once().delivered == 1
        _stamp, signatures = _signatures(env.recorder.requests[-1])
        assert len(signatures) == 2


@pytest.mark.req("EF-INT-005")
def test_webhook_old_secret_dropped_after_window(settings: Settings) -> None:
    with webhook_env(settings) as env:
        sub, first = env.subscribe()
        with write_transaction(env.engine) as connection:
            _, second = env.store.rotate(connection, sub.id, now=env.clock.now())
        env.clock.advance(hours=25)  # past the 24 h window

        env.publish()
        env.outbox().enqueue()
        assert env.deliverer().run_once().delivered == 1
        [request] = env.recorder.requests
        stamp, signatures = _signatures(request)
        assert signatures == [sign_body(second, int(stamp), request.content)]
        assert sign_body(first, int(stamp), request.content) not in signatures


@pytest.mark.req("EF-INT-005")
def test_the_previous_secret_leaves_the_sealed_document_once_expired(settings: Settings) -> None:
    with webhook_env(settings) as env:
        sub, first = env.subscribe()
        with write_transaction(env.engine) as connection:
            _, second = env.store.rotate(connection, sub.id, now=env.clock.now())
        env.clock.advance(hours=25)  # past the 24 h window: the next delivery sees it closed

        env.publish()
        env.outbox().enqueue()
        assert env.deliverer().run_once().delivered == 1
        [request] = env.recorder.requests
        _stamp, signatures = _signatures(request)
        assert signatures == [sign_body(second, int(_stamp), request.content)]
        # the sealed document no longer carries the previous secret (M6 L23a review)
        with env.engine.connect() as connection:
            document = json.loads(env.vault.open_row(connection, "webhook_subscription", sub.id))
        assert document == {"url": HOOK, "secret": second}
        assert first not in json.dumps(document)
        # a later delivery changes nothing: the document is already the plain pair
        env.publish("unit.stored", {"count": 5})
        env.outbox().enqueue()
        assert env.deliverer().run_once().delivered == 1
        with env.engine.connect() as connection:
            again = json.loads(env.vault.open_row(connection, "webhook_subscription", sub.id))
        assert again == document


@pytest.mark.req("EF-INT-005")
def test_a_subscription_that_was_never_rotated_signs_once(settings: Settings) -> None:
    with webhook_env(settings) as env:
        env.subscribe()
        env.publish()
        env.outbox().enqueue()
        assert env.deliverer().run_once().delivered == 1
        [request] = env.recorder.requests
        _stamp, signatures = _signatures(request)
        assert len(signatures) == 1


# -- the dead delivery event --------------------------------------------------------------------


@pytest.mark.req("EF-INT-005")
def test_webhook_dead_event_emitted_once(settings: Settings) -> None:
    with webhook_env(settings, respond=lambda _r: httpx2.Response(503)) as env:
        sub, _ = env.subscribe()
        event_id = env.publish()
        env.outbox().enqueue()
        bus = EventBus(env.clock)
        for _ in range(MAX_ATTEMPTS):
            env.deliverer(publisher=bus).run_once()
            env.clock.advance(hours=7)  # past the longest retry delay (6 h)
        [row] = env.rows()
        assert row["status"] == "dead"
        dead = env.events("webhook.dead")
        assert len(dead) == 1  # one event for one abandoned delivery, not one per attempt
        assert dead[0]["payload"] == {
            "subscription_id": sub.id,
            "event_id": event_id,
            "reason": "retries_exhausted",
        }
        # the row is dead: a later sweep neither sends it nor announces it again
        env.deliverer(publisher=bus).run_once()
        assert len(env.events("webhook.dead")) == 1
        # a slow copy that comes back after the lease cannot settle or announce a second time
        again = Due(row["id"], sub.id, event_id, 1, row["attempts"])
        env.outbox(publisher=bus).failed(again, status=503, error="http_status")
        assert len(env.events("webhook.dead")) == 1


@pytest.mark.req("EF-INT-005")
def test_webhook_dead_event_when_the_creator_is_no_longer_an_admin(settings: Settings) -> None:
    with webhook_env(settings) as env:
        sub, _ = env.subscribe()
        event_id = env.publish()
        env.outbox().enqueue()
        with write_transaction(env.engine) as connection:  # no longer theirs to send
            connection.execute(
                sa.text("UPDATE \"user\" SET role = 'member' WHERE id = :id"),
                {"id": env.admin_id},
            )  # fmt: skip
        bus = EventBus(env.clock)
        report = env.deliverer(publisher=bus).run_once()
        assert report.dead == 1
        dead = env.events("webhook.dead")
        assert len(dead) == 1
        # a reason of its own (M6 L23a review), not the one of an event that vanished
        assert dead[0]["payload"] == {
            "subscription_id": sub.id,
            "event_id": event_id,
            "reason": "not_theirs",
        }


@pytest.mark.req("EF-INT-005")
def test_webhook_dead_event_when_the_event_is_no_longer_exposed(settings: Settings) -> None:
    exposed = Catalogue([EventSpec(type="unit.stored", current=1, render={1: pick("count")})])
    with webhook_env(settings, catalogue=exposed) as env:
        sub, _ = env.subscribe()
        event_id = env.publish()
        env.outbox().enqueue()
        bus = EventBus(env.clock)
        # the delivery reads through a catalogue that no longer exposes the type: the event is
        # there, but there is no shape left to send it in
        report = Deliverer(
            env.engine, env.vault, env.clock, env.resolver,
            transport=env.recorder.transport, catalogue=Catalogue([]), publisher=bus,
        ).run_once()  # fmt: skip
        assert report.dead == 1
        dead = env.events("webhook.dead")
        assert len(dead) == 1
        assert dead[0]["payload"] == {
            "subscription_id": sub.id,
            "event_id": event_id,
            "reason": "event_gone",
        }


# -- the route: a reserved action, the secret shown once -----------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    resolver = FakeResolver(
        {"hooks.example.org": ["93.184.216.34"], "inside.example.org": ["10.0.0.5"]}
    )
    network = Network(resolver=resolver, transport=Recorder().transport)
    with auth_env(monkeypatch, settings, network=network) as env:
        assert env.login(ADMIN).status_code == 200
        yield env


@pytest.mark.req("EF-INT-005", "ENF-SEC-010")
def test_webhook_rotation_is_reserved(api: AuthEnv) -> None:
    made = _send(api, api.client, "POST", BASE, CREATE)
    assert made.status_code == 201, made.text
    sub_id = made.json()["id"]
    missing = new_ulid()
    for name, client in _refused(api).items():
        answer = _send(api, client, "POST", f"{BASE}/{sub_id}/rotate")
        assert answer.status_code in (401, 403), (name, answer.status_code)
        # IDOR: a subscription that exists and one that does not give one answer
        gone = _send(api, client, "POST", f"{BASE}/{missing}/rotate")
        assert (answer.status_code, answer.json().get("type")) == (
            gone.status_code,
            gone.json().get("type"),
        ), f"{name}: the answer tells whether the subscription exists"  # fmt: skip
        assert "hooks.example.org" not in answer.text, name
    # a key of scope `admin` reads; rotating is a reserved action: a fresh session, never a key
    admin_key = _bearer(api, _key(api, "admin"), "10.0.0.24")
    assert admin_key.get(BASE).status_code == 200
    assert _send(api, admin_key, "POST", f"{BASE}/{sub_id}/rotate").status_code in (401, 403)
    # the administrator's fresh session rotates, and the new secret comes back once
    turned = _send(api, api.client, "POST", f"{BASE}/{sub_id}/rotate")
    assert turned.status_code == 200, turned.text
    assert turned.json()["secret"] != made.json()["secret"]
    assert "no-store" in turned.headers.get("cache-control", "").lower()
    assert _send(api, api.client, "POST", f"{BASE}/{missing}/rotate").status_code == 404


@pytest.mark.req("EF-INT-005", "ENF-SEC-005")
def test_webhook_rotated_secret_is_shown_once(api: AuthEnv) -> None:
    made = _send(api, api.client, "POST", BASE, CREATE).json()
    turned = _send(api, api.client, "POST", f"{BASE}/{made['id']}/rotate")
    assert turned.status_code == 200, turned.text
    first, second = made["secret"], turned.json()["secret"]
    assert second != first and second.startswith("whsec_")
    for answer in (api.client.get(BASE), api.client.get(f"{BASE}/{made['id']}")):
        assert first not in answer.text and second not in answer.text
    # nor in any row in clear, nor in the audit, which names the subscription by its id
    dump = json.dumps(db_rows(api.settings, "SELECT * FROM webhook_subscription"), default=str)
    assert first not in dump and second not in dump
    audited = db_rows(
        api.settings, "SELECT action, target FROM audit WHERE action LIKE 'webhook.%' ORDER BY at"
    )
    assert [a[0] for a in audited] == ["webhook.subscribe", "webhook.rotate"]
    audit_dump = json.dumps(audited)
    assert first not in audit_dump and second not in audit_dump
    assert made["id"] in audit_dump and "hooks.example.org" not in audit_dump
