"""M4 L24 (EF-INT-005, EF-INT-002): the persisted outbox and the signed webhooks.

A webhook leaves only through the Fetcher and its SSRF guard, checked again at every delivery (a
name that answered a public address at registration may answer a private one later); its payload
is what `activity_view` gives the integration role, never a title; its secret is sealed and signs
each body. The outbox is rows: a process killed at any step loses nothing and sends nothing twice
under another identity. Synthetic hosts and addresses; secrets made at run time.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
from datetime import timedelta
from typing import Any

import httpx2
import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.events.activity import ActivityItem
from shelfhold.infrastructure.db import create_db_engine, write_transaction
from shelfhold.infrastructure.fetcher.errors import SsrfBlocked
from shelfhold.integrations.webhooks import (
    MAX_ATTEMPTS,
    WEBHOOK_EVENTS,
    Catalogue,
    CatalogueError,
    Deprecation,
    EventSpec,
    WebhookUrlRefused,
    check_target,
    pick,
    sign_body,
)
from tests.fakes_fetcher import FakeResolver
from tests.webhooks_env import HOOK, HOST, NOW, PUBLIC, Killed, WebhookEnv, webhook_env

CANARIES = ("/srv/synthetic-library/secret", "canary-host.invalid", "10.77.88.99", "Secret Title")


def _signature(request: httpx2.Request) -> tuple[str, str]:
    header = request.headers["x-shelfhold-signature"]
    parts = dict(p.split("=", 1) for p in header.split(","))
    assert set(parts) == {"t", "v1"}, header
    return parts["t"], parts["v1"]


# -- the outbox, the signature, the schema version ------------------------------------------------


@pytest.mark.req("EF-INT-005")
def test_outbox_persisted_webhook_hmac_schema_version(settings: Settings) -> None:
    with webhook_env(settings) as env:
        sub, secret = env.subscribe()
        env.publish("unit.stored", {"count": 2, "edition_id": "01HZZZZZZZZZZZZZZZZZZZZZZA"})
        assert env.outbox().enqueue() == 1
        # the row is in the database, not in memory: a second engine reads it
        other = create_db_engine(settings.database_path)
        try:
            with other.connect() as connection:
                pending = connection.execute(
                    sa.text("SELECT status, schema_version, attempts FROM webhook_outbox")
                ).all()
        finally:
            other.dispose()
        assert [tuple(r) for r in pending] == [("pending", 1, 0)]

        report = env.deliverer().run_once()
        assert report.delivered == 1
        [request] = env.recorder.requests
        assert request.method == "POST" and request.url.host == HOST
        stamp, digest = _signature(request)
        expected = hmac.new(secret.encode(), f"{stamp}.".encode() + request.content, hashlib.sha256)
        assert hmac.compare_digest(digest, expected.hexdigest())
        assert digest == sign_body(secret, int(stamp), request.content)
        body = json.loads(request.content)
        assert body["type"] == "unit.stored" and body["schema_version"] == 1
        assert body["data"] == {"count": 2, "edition_id": "01HZZZZZZZZZZZZZZZZZZZZZZA"}
        assert request.headers["x-shelfhold-schema-version"] == "1"
        row = env.rows()[0]
        assert row["status"] == "delivered" and request.headers["x-shelfhold-delivery"] == row["id"]
        assert int(stamp) == int(NOW.timestamp())
        assert sub.id == row["subscription_id"]


@pytest.mark.req("EF-INT-005")
def test_the_signature_covers_the_timestamp_and_the_body(settings: Settings) -> None:
    secret = "k" * 32
    body = b'{"a":1}'
    good = sign_body(secret, 1_700_000_000, body)
    assert sign_body(secret, 1_700_000_001, body) != good  # a replay at another time
    assert sign_body(secret, 1_700_000_000, body + b" ") != good
    assert sign_body("j" * 32, 1_700_000_000, body) != good


@pytest.mark.req("EF-INT-005")
def test_failed_delivery_is_retried_with_exponential_delay_then_dead(settings: Settings) -> None:
    with webhook_env(settings, respond=lambda _r: httpx2.Response(503)) as env:
        env.subscribe()
        env.publish()
        env.outbox().enqueue()
        delays: list[float] = []
        for attempt in range(1, MAX_ATTEMPTS + 1):
            report = env.deliverer().run_once()
            [row] = env.rows()
            assert row["attempts"] == attempt and report.delivered == 0
            if attempt < MAX_ATTEMPTS:
                assert row["status"] == "pending" and row["last_status"] == 503
                gap = row["next_try_at"].replace(tzinfo=NOW.tzinfo) - env.clock.now()
                delays.append(gap.total_seconds())
                # not due yet: nothing is sent before the delay
                before = len(env.recorder.requests)
                env.deliverer().run_once()
                assert len(env.recorder.requests) == before
                env.clock.advance(seconds=gap.total_seconds())
        assert delays[1] == 2 * delays[0] and delays[2] == 2 * delays[1]
        assert env.rows()[0]["status"] == "dead"
        env.clock.advance(days=30)
        sent = len(env.recorder.requests)
        env.deliverer().run_once()
        assert len(env.recorder.requests) == sent  # a dead row is never sent again


@pytest.mark.req("EF-INT-005")
def test_outbox_survives_kill9_and_replay_is_idempotent(settings: Settings) -> None:
    with webhook_env(settings) as env:
        env.subscribe()
        first = env.publish()
        env.outbox().enqueue()
        env.outbox().enqueue()  # a second sweep: the same event is the same row
        assert len(env.rows()) == 1

        def killed(_request: httpx2.Request) -> httpx2.Response:
            raise Killed

        env.recorder._respond = killed  # the process dies while the request is on the wire
        with pytest.raises(Killed):
            env.deliverer().run_once()
        [row] = env.rows()
        assert row["status"] == "pending" and row["attempts"] == 1  # claimed, not settled
        env.recorder._respond = lambda _r: httpx2.Response(200)
        env.deliverer().run_once()  # not due: the claim holds the row for a while
        assert env.rows()[0]["status"] == "pending"
        env.clock.advance(hours=1)
        fresh = create_db_engine(settings.database_path)  # a new process, the same database
        try:
            env.deliverer(fresh).run_once()
        finally:
            fresh.dispose()
        [row] = env.rows()
        assert row["status"] == "delivered" and row["attempts"] == 2
        ids = {r.headers["x-shelfhold-delivery"] for r in env.recorder.requests}
        assert ids == {row["id"]}  # the receiver can drop the second copy: one identity
        env.outbox().enqueue()  # the event was already seen: nothing more is made of it
        assert len(env.rows()) == 1 and first == row["event_id"]


@pytest.mark.req("EF-INT-005")
def test_the_cursor_moves_with_the_rows_so_a_crash_loses_nothing(settings: Settings) -> None:
    with webhook_env(settings) as env:
        sub, _ = env.subscribe()
        env.publish("unit.stored")
        env.publish("unit.stored", {"count": 5})
        outbox = env.outbox()
        real = outbox._insert

        def failing(*args: Any, **kwargs: Any) -> None:
            real(*args, **kwargs)
            raise Killed  # between the first insert and the cursor: one transaction, all undone

        outbox._insert = failing  # type: ignore[method-assign]
        with pytest.raises(Killed):
            outbox.enqueue()
        assert env.rows() == []
        assert env.outbox().enqueue() == 2  # nothing was skipped
        assert len(env.rows()) == 2 and sub.id


@pytest.mark.req("EF-INT-005")
def test_only_events_after_the_subscription_and_of_its_types(settings: Settings) -> None:
    with webhook_env(settings) as env:
        env.publish("unit.stored", {"count": 1})  # before: never sent
        env.subscribe(types=("unit.stored",))
        env.publish("disk.low", {"count": 2})  # another type
        env.publish("unit.stored", {"count": 3})
        env.outbox().enqueue()
        env.deliverer().run_once()
        assert [b["data"]["count"] for b in env.bodies()] == [3]


# -- the SSRF guard ---------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-005", "ENF-SEC-001")
@pytest.mark.parametrize(
    "url",
    [
        "https://169.254.169.254/latest/meta-data",
        "http://169.254.169.254/",
        "https://127.0.0.1/hook",
        "https://[::1]/hook",
        "https://10.0.0.5/hook",
        "https://inside.example.org/hook",  # a name that answers a private address
        "https://2130706433/hook",  # decimal notation of 127.0.0.1
        "https://0x7f.0.0.1/hook",
        "https://[::ffff:127.0.0.1]/hook",
        f"https://user:pass@{HOST}/hook",  # a credential in the address
        f"https://{HOST}/hook#frag",
        f"http://{HOST}/hook",  # a public address is https only
        "ftp://hooks.example.org/hook",
        "file:///etc/passwd",
        "https:///nohost",
        "",
    ],
)
def test_webhook_url_goes_through_ssrf_guard(settings: Settings, url: str) -> None:
    with webhook_env(settings) as env:
        with pytest.raises((WebhookUrlRefused, SsrfBlocked)):
            env.subscribe(url)
        assert env.recorder.requests == []
        with env.engine.connect() as connection:
            assert connection.execute(sa.text("SELECT count(*) FROM webhook_subscription")).scalar() == 0


@pytest.mark.req("EF-INT-005", "ENF-SEC-001")
def test_a_public_https_url_is_accepted_and_the_private_one_only_when_the_admin_says_so(
    settings: Settings,
) -> None:
    with webhook_env(settings, dns={"nas.example.lan": ["192.168.1.20"]}) as env:
        env.subscribe(HOOK)
        with pytest.raises(SsrfBlocked):
            env.subscribe("http://nas.example.lan:8080/hook")  # private: refused by default
        sub, _ = env.subscribe("http://nas.example.lan:8080/hook", allow_private=True)
        assert sub.private_target == "nas.example.lan:8080"
        # the metadata service is never reachable, whatever the admin allows
        with pytest.raises((WebhookUrlRefused, SsrfBlocked)):
            env.subscribe("http://169.254.169.254/", allow_private=True)
        # allowing a private target is that host and that port, not the whole network
        env.resolver.table["other.example.lan"] = ["192.168.1.21"]
        env.publish()
        env.outbox().enqueue()
        env.deliverer().run_once()
        assert {r.headers["host"] for r in env.recorder.requests} == {HOST, "nas.example.lan:8080"}


@pytest.mark.req("EF-INT-005", "ENF-SEC-001")
def test_webhook_delivery_rechecks_resolved_address(settings: Settings) -> None:
    """DNS rebinding: the name is public at registration and private at delivery. The guard runs
    at every delivery; nothing is sent, and the row records a refusal, not a success."""
    answers = [[PUBLIC], ["10.0.0.5"], ["169.254.169.254"], [PUBLIC]]
    with webhook_env(settings, dns={HOST: answers}) as env:
        env.subscribe()  # the first answer: public
        env.publish()
        env.outbox().enqueue()
        first = env.deliverer().run_once()
        assert first.refused == 1 and env.recorder.requests == []
        [row] = env.rows()
        assert row["status"] == "pending" and row["last_error"] == "ssrf.blocked"
        env.clock.advance(hours=1)
        second = env.deliverer().run_once()  # metadata address now
        assert second.refused == 1 and env.recorder.requests == []
        env.clock.advance(hours=3)
        third = env.deliverer().run_once()  # public again: it goes
        assert third.delivered == 1 and len(env.recorder.requests) == 1


@pytest.mark.req("EF-INT-005", "ENF-SEC-001")
def test_a_private_target_keeps_its_declared_addresses(settings: Settings) -> None:
    """Allowed for `nas.example.lan:8080` as it resolved at registration: a record that moves to
    another machine of the network, or to the metadata address, is refused at delivery."""
    answers = [["192.168.1.20"], ["192.168.1.20"], ["192.168.1.99"], ["169.254.169.254"]]
    with webhook_env(settings, dns={"nas.example.lan": answers}) as env:
        env.subscribe("http://nas.example.lan:8080/hook", allow_private=True)
        env.publish()
        env.publish()
        env.publish()
        env.outbox().enqueue()
        report = env.deliverer().run_once()
        assert report.delivered == 1 and report.refused == 2
        assert len(env.recorder.requests) == 1


@pytest.mark.req("EF-INT-005", "ENF-SEC-001")
@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize(
    "target", ["http://127.0.0.1/admin", "http://169.254.169.254/latest", "http://[::1]/x",
               "http://inside.example.org/x"],
)
def test_a_redirect_to_a_private_address_is_not_followed(
    settings: Settings, status: int, target: str
) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, headers={"location": target})

    with webhook_env(settings, respond=respond) as env:
        env.subscribe()
        env.publish()
        env.outbox().enqueue()
        env.deliverer().run_once()
        assert len(env.recorder.requests) == 1  # the first hop only, never the redirect target
        [row] = env.rows()
        assert row["status"] == "pending" and row["last_status"] != 200
        assert row["attempts"] == 1


@pytest.mark.req("EF-INT-005")
def test_a_redirect_never_carries_the_signature_elsewhere(settings: Settings) -> None:
    def respond(request: httpx2.Request) -> httpx2.Response:
        if request.headers["host"] == HOST:
            return httpx2.Response(302, headers={"location": "https://other.example.org/x"})
        return httpx2.Response(200)

    with webhook_env(settings, dns={"other.example.org": [PUBLIC]}, respond=respond) as env:
        env.subscribe()
        env.publish()
        env.outbox().enqueue()
        env.deliverer().run_once()
        for request in env.recorder.requests[1:]:
            assert "x-shelfhold-signature" not in request.headers
            assert not request.content


# -- the payload goes through activity_view ------------------------------------------------------------


@pytest.mark.req("EF-INT-005", "EF-USR-016")
def test_webhook_payload_goes_through_activity_view(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    import shelfhold.events.projection as projection

    seen: list[tuple[str, str]] = []
    real = projection.activity_view

    def spy(viewer: Any, event: Any) -> ActivityItem | None:
        seen.append((viewer.role, event.visibility))
        return real(viewer, event)

    monkeypatch.setattr(projection, "activity_view", spy)
    with webhook_env(settings) as env:
        child = env.add_user("child")
        env.subscribe(types=("unit.stored",))
        env.publish("unit.stored", {"count": 1}, visibility="private", actor_id=child)
        env.publish(
            "unit.stored",
            {"count": 2, "title": "Secret Title", "path": CANARIES[0], "host": CANARIES[1],
             "ip": CANARIES[2], "user_id": child, "owner_id": child},
            visibility="household", actor_id=child,
        )  # fmt: skip
        env.publish("unit.stored", {"count": 3}, visibility="ops")
        env.outbox().enqueue()
        env.deliverer().run_once()
        assert seen and {role for role, _ in seen} == {"integration"}
        counts = sorted(b["data"]["count"] for b in env.bodies())
        assert counts == [2, 3]  # the private one never left
        raw = b"".join(r.content for r in env.recorder.requests).decode()
        for canary in (*CANARIES, child):
            assert canary not in raw
        for body in env.bodies():
            assert "title" not in body and "actor_id" not in body and "user_id" not in body["data"]


@pytest.mark.req("EF-INT-005", "EF-USR-016")
def test_a_subscription_without_its_creator_sends_nothing(settings: Settings) -> None:
    """The reader of a webhook is its creator's profile: when they are gone, the scope is nobody's
    and nothing leaves (closed by default)."""
    with webhook_env(settings) as env:
        env.subscribe()
        env.publish()
        with write_transaction(env.engine) as connection:
            connection.execute(sa.text("UPDATE webhook_subscription SET created_by = NULL"))
        env.outbox().enqueue()
        env.deliverer().run_once()
        assert env.recorder.requests == []


# -- the schema version: upcast on read, double emission during a deprecation --------------------------


def _v1_to_v2(payload: dict[str, Any]) -> dict[str, Any]:
    return {**payload, "state": payload.pop("status", "unknown")}


def _demo_catalogue(since: Any = NOW, window_days: int = 90) -> Catalogue:
    return Catalogue(
        [
            EventSpec(
                type="unit.stored",
                current=2,
                render={1: pick("count", "status"), 2: pick("count", "state")},
                deprecated={1: Deprecation(since=since, sunset=since + timedelta(days=window_days))},
            )
        ]
    )


@pytest.mark.req("EF-INT-005")
def test_deprecated_event_double_emitted(settings: Settings) -> None:
    with webhook_env(settings, catalogue=_demo_catalogue()) as env:
        env.subscribe()
        env.publish("unit.stored", {"count": 1, "status": "ok", "state": "ok"})
        env.outbox().enqueue()
        assert [r["schema_version"] for r in env.rows()] == [1, 2]  # both, for the same event
        assert len({r["event_id"] for r in env.rows()}) == 1
        env.deliverer().run_once()
        by_version = {b["schema_version"]: b["data"] for b in env.bodies()}
        assert by_version == {1: {"count": 1, "status": "ok"}, 2: {"count": 1, "state": "ok"}}
        assert {r.headers["x-shelfhold-schema-version"] for r in env.recorder.requests} == {"1", "2"}
        assert len({r.headers["x-shelfhold-delivery"] for r in env.recorder.requests}) == 2
        # after the sunset, only the current version is made
        env.clock.advance(days=91)
        env.publish("unit.stored", {"count": 2, "status": "ok", "state": "ok"})
        env.outbox().enqueue()
        later = [r for r in env.rows() if r["event_id"] != env.rows()[0]["event_id"]]
        assert [r["schema_version"] for r in later] == [2]


@pytest.mark.req("EF-INT-005")
def test_double_emission_is_bounded() -> None:
    long = timedelta(days=181)
    with pytest.raises(CatalogueError):  # a window too long: a deprecation is not a way of life
        Catalogue([EventSpec("a.b", 3, {1: pick(), 2: pick(), 3: pick()},
                             {1: Deprecation(NOW, NOW + long)})])  # fmt: skip
    with pytest.raises(CatalogueError):  # two old versions at once
        Catalogue([EventSpec("a.b", 3, {1: pick(), 2: pick(), 3: pick()},
                             {1: Deprecation(NOW, NOW + timedelta(days=30)),
                              2: Deprecation(NOW, NOW + timedelta(days=30))})])  # fmt: skip
    with pytest.raises(CatalogueError):  # a version that has no shape
        Catalogue([EventSpec("a.b", 2, {2: pick()}, {1: Deprecation(NOW, NOW + timedelta(days=1))})])
    with pytest.raises(CatalogueError):  # the sunset is before the start
        Catalogue([EventSpec("a.b", 2, {1: pick(), 2: pick()},
                             {1: Deprecation(NOW, NOW - timedelta(days=1))})])  # fmt: skip
    for spec in WEBHOOK_EVENTS.specs():  # the real catalogue obeys the same bounds
        assert len(spec.deprecated) <= 1 and spec.current in spec.render


@pytest.mark.req("EF-INT-002", "EF-INT-005")
def test_event_upcast_for_each_retained_schema_version(settings: Settings) -> None:
    """A row keeps the version it was written with; the outbox presents it at the current one
    (`events.upcast`), whichever retained version it is. A version nobody can read is skipped, and
    the others behind it still go."""
    from shelfhold.events import integration_feed

    steps = {
        ("unit.stored", 1): lambda p: {**p, "state": p.pop("status", "unknown")},
        ("unit.stored", 2): lambda p: {**p, "count": int(p["count"]) * 10},
    }
    with webhook_env(settings) as env:
        sub, _ = env.subscribe()
        for version in (1, 2, 3):
            env.publish("unit.stored", {"count": version, "status": "ok", "state": "ok"},
                        schema_version=version)  # fmt: skip
        env.publish("unit.stored", {"count": 9}, schema_version=4)  # from a later release
        env.publish("unit.stored", {"count": 8}, schema_version=3)
        with env.engine.connect() as connection:
            feed, cursor = integration_feed.read_after(
                connection, 0, as_user=env.admin_id, current=3, steps=steps
            )
        assert [(e.stored_version, e.payload["count"]) for e in feed] == [
            (1, 10), (2, 20), (3, 3), (3, 8),
        ]  # every retained version reaches the current shape; the unreadable one is skipped
        assert all("state" in e.payload for e in feed[:2])
        assert cursor == feed[-1].event_id
        assert sub.id
        assert integration_feed.RETAINED_VERSIONS == (1,)  # the bus writes version 1 today


@pytest.mark.req("EF-INT-002")
def test_a_row_of_a_later_release_does_not_stop_the_outbox(settings: Settings) -> None:
    with webhook_env(settings) as env:
        env.subscribe()
        env.publish("unit.stored", {"count": 1}, schema_version=7)
        env.publish("unit.stored", {"count": 2})
        env.outbox().enqueue()
        env.deliverer().run_once()
        assert [b["data"]["count"] for b in env.bodies()] == [2]


# -- the secret ------------------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-005", "ENF-SEC-005")
def test_the_secret_and_the_url_are_sealed_and_never_logged(
    settings: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    import structlog

    with structlog.testing.capture_logs() as logs, webhook_env(settings) as env:
        sub, secret = env.subscribe()
        env.publish()
        env.outbox().enqueue()
        env.deliverer().run_once()
        with env.engine.connect() as connection:
            dump = json.dumps(
                [
                    [str(c) for c in row]
                    for table in ("webhook_subscription", "webhook_outbox")
                    for row in connection.execute(sa.text(f"SELECT * FROM {table}"))  # noqa: S608
                ]
            )
        assert secret not in dump and "/in/one" not in dump  # sealed, at rest
        assert sub.url_host == HOST
        assert secret not in repr(sub) and secret not in str(sub)
        opened = env.store.open(env.engine, sub.id)
        assert opened.secret == secret and opened.url == HOOK
        assert secret not in repr(opened)
    assert secret not in json.dumps(logs, default=str)
    assert secret not in capsys.readouterr().out + capsys.readouterr().err


@pytest.mark.req("EF-INT-005", "ENF-SEC-005")
def test_the_secret_is_bound_to_its_row(settings: Settings) -> None:
    """An envelope copied from one subscription to another does not open."""
    with webhook_env(settings) as env:
        one, _ = env.subscribe()
        two, _ = env.subscribe("https://hooks.example.org/in/two")
        with write_transaction(env.engine) as connection:
            columns = ("secret_kid", "secret_v", "secret_nonce", "secret_ct")
            row = connection.execute(
                sa.text(f"SELECT {', '.join(columns)} FROM webhook_subscription WHERE id = :i"),  # noqa: S608
                {"i": one.id},
            ).one()
            connection.execute(
                sa.text(
                    "UPDATE webhook_subscription SET secret_kid=:a, secret_v=:b, secret_nonce=:c, "
                    "secret_ct=:d WHERE id = :i"
                ),
                {"a": row[0], "b": row[1], "c": row[2], "d": row[3], "i": two.id},
            )
        with pytest.raises(Exception):  # noqa: B017, PT011  (an authentication failure)
            env.store.open(env.engine, two.id)


@pytest.mark.req("EF-INT-005")
def test_the_catalogue_is_a_closed_list_and_a_type_outside_it_is_refused(
    settings: Settings,
) -> None:
    with webhook_env(settings) as env:
        with pytest.raises(WebhookUrlRefused):
            env.subscribe(types=("auth.passkey_registered",))  # a person's event: never exposed
        with pytest.raises(WebhookUrlRefused):
            env.subscribe(types=())
        with pytest.raises(WebhookUrlRefused):
            env.subscribe(types=("plugin.someone.thing",))
        assert "auth.passkey_registered" not in {s.type for s in WEBHOOK_EVENTS.specs()}


def test_check_target_is_a_coroutine_that_resolves_through_the_given_resolver() -> None:
    resolver = FakeResolver({HOST: [PUBLIC]})
    target = asyncio.run(check_target(HOOK, allow_private=False, resolver=resolver))
    assert (target.host, target.port, target.private) == (HOST, 443, False)
    assert resolver.calls
