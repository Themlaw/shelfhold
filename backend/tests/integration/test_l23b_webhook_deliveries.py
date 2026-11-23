"""M6 L23b (EF-INT-005): the last deliveries of a webhook, read by the administrator alone.

The screen of the webhooks shows, under each subscription, what became of its last deliveries:
the identity of the row (the receiver's idempotency key), the event, the version of the shape,
the state (`pending`, `delivered`, `dead`), the attempts, the HTTP status of the last try and its
closed code. Reading them is the administrator's alone, like the list: a child, a member, a
read-only key and nobody are refused before anything is read, and a subscription that exists and
one that does not give one answer. Nothing of the address, of its path or of the secret is in the
answer, and the deliveries of one subscription never come with those of another.

Synthetic hosts and addresses; the rows are made here, in the database, the way the outbox makes
them.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.composition import Network
from shelfhold.config import Settings
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import platform
from shelfhold.infrastructure.schema import webhooks as schema
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

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
DELIVERIES = "{base}/{sub}/deliveries"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[AuthEnv]:
    resolver = FakeResolver({"hooks.example.org": ["93.184.216.34"]})
    network = Network(resolver=resolver, transport=Recorder().transport)
    with auth_env(monkeypatch, settings, network=network) as env:
        assert env.login(ADMIN).status_code == 200
        yield env


def _subscribe(api: AuthEnv, url: str = "https://hooks.example.org/in/one") -> str:
    made = _send(api, api.client, "POST", BASE, {**CREATE, "url": url})
    assert made.status_code == 201, made.text
    return str(made.json()["id"])


def _deliver(
    api: AuthEnv,
    subscription_id: str,
    *,
    count: int = 1,
    status: str = "delivered",
    attempts: int = 1,
    last_status: int | None = 200,
    last_error: str | None = None,
) -> list[int]:
    """Rows the way the outbox makes them: an event, then the delivery of it to one address."""
    made: list[int] = []
    engine = create_db_engine(api.settings.database_path)
    try:
        with write_transaction(engine) as connection:
            for step in range(count):
                event_id = int(
                    connection.execute(
                        sa.insert(platform.event).values(
                            type="unit.stored",
                            schema_version=1,
                            payload={"count": step},
                            occurred_at=NOW + timedelta(minutes=step),
                            visibility="ops",
                        )
                    ).inserted_primary_key[0]
                )
                connection.execute(
                    sa.insert(schema.webhook_outbox).values(
                        id=new_ulid(),
                        subscription_id=subscription_id,
                        event_id=event_id,
                        schema_version=1,
                        status=status,
                        attempts=attempts,
                        next_try_at=NOW + timedelta(minutes=step),
                        last_status=last_status,
                        last_error=last_error,
                        delivered_at=NOW + timedelta(minutes=step)
                        if status == "delivered"
                        else None,
                    )
                )
                made.append(event_id)
    finally:
        engine.dispose()
    return made


def _deliveries(api: AuthEnv, subscription_id: str, client: Any = None) -> Any:
    return _send(api, client or api.client, "GET", DELIVERIES.format(base=BASE, sub=subscription_id))


@pytest.mark.req("EF-INT-005")
def test_the_last_deliveries_of_a_subscription_are_read_newest_first(api: AuthEnv) -> None:
    sub_id = _subscribe(api)
    delivered = _deliver(api, sub_id, count=3)
    retried = _deliver(
        api, sub_id, status="pending", attempts=2, last_status=503, last_error="http_status"
    )
    dead = _deliver(api, sub_id, status="dead", attempts=8, last_status=None, last_error="timeout")

    answer = _deliveries(api, sub_id)
    assert answer.status_code == 200, answer.text
    items = answer.json()["items"]
    assert len(items) == 5
    assert [item["event_id"] for item in items] == [*dead, *retried, *reversed(delivered)]
    newest = items[0]
    assert set(newest) == {
        "id", "event_id", "schema_version", "status", "attempts", "last_status",
        "last_error", "delivered_at", "next_try_at",
    }  # fmt: skip
    assert newest["status"] == "dead" and newest["attempts"] == 8
    assert newest["last_status"] is None and newest["last_error"] == "timeout"
    assert newest["delivered_at"] is None
    retrying = items[1]
    assert (retrying["status"], retrying["attempts"]) == ("pending", 2)
    assert (retrying["last_status"], retrying["last_error"]) == (503, "http_status")
    assert items[2]["status"] == "delivered" and items[2]["delivered_at"] is not None
    assert items[2]["schema_version"] == 1
    # the identity of the delivery is the receiver's idempotency key: it is shown, and it is a ULID
    assert len(newest["id"]) == 26


@pytest.mark.req("EF-INT-005", "ENF-SEC-005")
def test_the_deliveries_carry_no_address_and_no_secret(api: AuthEnv) -> None:
    made = _send(api, api.client, "POST", BASE, CREATE)
    sub_id = made.json()["id"]
    secret = made.json()["secret"]
    _deliver(api, sub_id, status="pending", attempts=3, last_status=500, last_error="http_status")

    answer = _deliveries(api, sub_id)
    assert answer.status_code == 200
    assert secret not in answer.text
    assert "/in/one" not in answer.text  # the path of the address may be a token
    assert "hooks.example.org" not in answer.text
    assert "secret" not in answer.text.lower()


@pytest.mark.req("EF-INT-005")
def test_the_deliveries_of_one_subscription_never_come_with_those_of_another(api: AuthEnv) -> None:
    mine = _subscribe(api, "https://hooks.example.org/in/one")
    other = _subscribe(api, "https://hooks.example.org/in/two")
    mine_events = _deliver(api, mine, count=2)
    other_events = _deliver(api, other, count=3)

    answer = _deliveries(api, mine)
    assert answer.status_code == 200
    assert [item["event_id"] for item in answer.json()["items"]] == sorted(mine_events, reverse=True)
    assert not set(other_events) & {item["event_id"] for item in answer.json()["items"]}


@pytest.mark.req("EF-INT-005")
def test_the_deliveries_are_capped_to_the_most_recent(api: AuthEnv) -> None:
    sub_id = _subscribe(api)
    events = _deliver(api, sub_id, count=25)

    answer = _deliveries(api, sub_id)
    assert answer.status_code == 200
    items = answer.json()["items"]
    assert len(items) == 20  # the screen shows the last ones, not the whole history
    assert [item["event_id"] for item in items] == sorted(events, reverse=True)[:20]


@pytest.mark.req("EF-INT-005", "ENF-SEC-010")
def test_the_deliveries_are_refused_to_child_member_and_non_admin_key(api: AuthEnv) -> None:
    sub_id = _subscribe(api)
    _deliver(api, sub_id, count=2)
    missing = new_ulid()
    for name, client in _refused(api).items():
        answer = _deliveries(api, sub_id, client)
        assert answer.status_code in (401, 403), (name, answer.status_code)
        assert sub_id not in answer.text and "hooks.example.org" not in answer.text, name
        # IDOR: a subscription that exists and one that does not give one answer
        none = _deliveries(api, missing, client)
        assert (answer.status_code, answer.json().get("type")) == (
            none.status_code, none.json().get("type"),
        ), f"{name}: the answer tells whether the subscription exists"  # fmt: skip
    # a key of scope `admin` reads them, like the list
    admin_key = _bearer(api, _key(api, "admin"), "10.0.0.24")
    assert _deliveries(api, sub_id, admin_key).status_code == 200
    assert _deliveries(api, missing, admin_key).status_code == 404


@pytest.mark.req("EF-INT-005")
def test_the_deliveries_of_a_subscription_that_is_not_there_are_not_found(api: AuthEnv) -> None:
    answer = _deliveries(api, new_ulid())
    assert answer.status_code == 404
    assert answer.json()["code"] == "webhook.not_found"
    assert db_rows(api.settings, "SELECT count(*) FROM webhook_outbox")[0][0] == 0


@pytest.mark.req("EF-INT-005")
def test_a_subscription_without_a_delivery_answers_an_empty_list(api: AuthEnv) -> None:
    sub_id = _subscribe(api)
    answer = _deliveries(api, sub_id)
    assert answer.status_code == 200
    assert answer.json() == {"items": []}
