"""EF-INT-006, ENF-OBS-005: ops events are notified from a row written with the event."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.events.bus import EventPayloadError, UnknownEventType
from shelfhold.events.operating import NOT_NOTIFIED, OPERATING_EVENTS
from shelfhold.infrastructure.fetcher import LanTargets
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.logging import forget_secret_values
from shelfhold.infrastructure.schema import platform
from shelfhold.integrations.apprise import (
    ChannelUrlRefused,
    Message,
    NotificationDispatcher,
    SendResult,
    ValidatedChannel,
    register_notification_job,
    request_dispatch,
    validate_channel_url,
)
from shelfhold.jobs.huey_app import JobPools
from tests.notifications_env import CHANNEL_URL, TOKEN, NotifyEnv, ScriptedSend, notify_env

pending = platform.notification_pending
event = platform.event


@pytest.fixture
def env(settings: Settings) -> Iterator[NotifyEnv]:
    with notify_env(settings) as built:
        yield built


def count(env: NotifyEnv, table: sa.Table) -> int:
    with env.engine.connect() as connection:
        return connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()


def dispatcher(env: NotifyEnv, send: ScriptedSend) -> NotificationDispatcher:
    return NotificationDispatcher(env.engine, env.vault, env.clock, send=send, bus=env.bus)


@pytest.mark.req("EF-INT-006", "ENF-OBS-005")
def test_ops_events_notified_by_default(env: NotifyEnv) -> None:
    env.add_channel()
    notified = sorted(OPERATING_EVENTS - NOT_NOTIFIED)
    with env.engine.begin() as connection:
        for event_type in sorted(OPERATING_EVENTS):
            env.bus.publish(connection, event_type, {})
    with env.engine.connect() as connection:
        rows = connection.execute(
            sa.select(event.c.type)
            .select_from(pending.join(event, event.c.id == pending.c.event_id))
            .order_by(event.c.type)
        ).all()
    assert [r.type for r in rows] == notified
    assert {"notify.failed"} == NOT_NOTIFIED  # the banner, never sent by Apprise
    assert "backup.failed" in notified and "disk.low" in notified


@pytest.mark.req("EF-INT-006")
def test_no_channel_no_pending_row_and_other_events_are_not_notified(env: NotifyEnv) -> None:
    with env.engine.begin() as connection:
        env.bus.publish(connection, "disk.low", {})
    assert count(env, event) == 1
    assert count(env, pending) == 0
    env.add_channel()
    with env.engine.begin() as connection:
        env.bus.publish(connection, "ops.window_missed", {"job_kind": "x"})
        env.bus.publish(connection, "disk.low", {}, visibility="private")
    assert count(env, pending) == 0
    with env.engine.begin() as connection, pytest.raises(UnknownEventType):
        env.bus.publish(connection, "disk.nearly_low", {})


@pytest.mark.req("EF-INT-006")
def test_event_and_pending_row_share_one_transaction(env: NotifyEnv) -> None:
    env.add_channel()
    with pytest.raises(RuntimeError), env.engine.begin() as connection:
        env.bus.publish(connection, "backup.failed", {})
        raise RuntimeError("the unit of work fails after the event")
    assert count(env, event) == 0
    assert count(env, pending) == 0
    with env.engine.begin() as connection:
        env.bus.publish(connection, "backup.failed", {})
    assert count(env, event) == 1
    assert count(env, pending) == 1


@pytest.mark.req("EF-INT-006")
def test_notification_survives_crash_between_event_and_send(env: NotifyEnv) -> None:
    env.add_channel()
    with env.engine.begin() as connection:
        env.bus.publish(connection, "disk.low", {"count": 1})
    # the process dies here: nothing was sent, the job queue is gone. A new process finds the row.
    send = ScriptedSend()
    report = dispatcher(env, send).run_once()
    assert report.sent == 1
    assert len(send.sent) == 1
    assert send.sent[0][0] == CHANNEL_URL
    assert count(env, pending) == 0  # purged after success
    assert dispatcher(env, send).run_once().sent == 0  # idempotent: the second run sends nothing
    assert len(send.sent) == 1


@pytest.mark.req("EF-INT-006")
def test_failed_send_is_retried_then_reported_as_notify_failed(env: NotifyEnv) -> None:
    env.add_channel()
    with env.engine.begin() as connection:
        env.bus.publish(connection, "disk.low", {})
    send = ScriptedSend()
    send.ok = False
    work = dispatcher(env, send)
    assert work.run_once().failed == 1
    with env.engine.connect() as connection:
        row = connection.execute(sa.select(pending)).one()
    assert row.attempts == 1
    assert row.next_try_at > env.clock.now()
    assert work.run_once().sent == 0  # not due yet
    assert len(send.sent) == 1
    for _ in range(10):
        env.clock.advance(hours=2)
        work.run_once()
    assert count(env, pending) == 0  # given up: bounded retries
    with env.engine.connect() as connection:
        types = [r.type for r in connection.execute(sa.select(event.c.type))]
    assert types.count("notify.failed") == 1  # the banner event, itself never sent again


@pytest.mark.req("EF-INT-006")
def test_dead_letters_are_aggregated_in_one_message(env: NotifyEnv) -> None:
    env.add_channel()
    with env.engine.begin() as connection:
        for _ in range(4):
            env.bus.publish(connection, "job.dead_lettered", {"job_id": new_ulid()})
    send = ScriptedSend()
    assert dispatcher(env, send).run_once().sent == 4
    assert len(send.sent) == 1
    assert "4" in send.sent[0][1].body
    assert count(env, pending) == 0


@pytest.mark.req("EF-INT-006", "ENF-SEC-005")
def test_channel_url_is_sealed_and_never_logged(
    env: NotifyEnv, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    env.add_channel()
    with env.engine.connect() as connection:
        row = connection.execute(
            sa.select(sa.text("settings, secret_ct")).select_from(sa.text("source_config"))
        ).one()
    assert TOKEN.encode() not in bytes(row.secret_ct)
    assert TOKEN not in str(row.settings)
    assert env.store.names(env.engine) == ["ops"]
    with env.engine.begin() as connection:
        env.bus.publish(connection, "disk.low", {})
    send = ScriptedSend()
    send.ok = False
    dispatcher(env, send).run_once()
    assert TOKEN not in caplog.text
    forget_secret_values()


@pytest.mark.req("ENF-SEC-015", "EF-INT-006")
def test_events_jobs_logs_carry_no_title(env: NotifyEnv, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    env.add_channel()
    title = "Berserk Deluxe Edition"
    with env.engine.begin() as connection:
        for payload in (
            {"title": title},
            {"reason": title},
            {"series_id": title},
            {"nested": {"summary": "x"}},
            {"job_id": new_ulid(), "detail": "free text"},
        ):
            with pytest.raises(EventPayloadError):
                env.bus.publish(connection, "source.broken", payload)
        env.bus.publish(
            connection,
            "source.broken",
            {"source_id": new_ulid(), "reason": "http_503", "count": 3},
        )
    with env.engine.connect() as connection:
        payloads = [str(r.payload) for r in connection.execute(sa.select(event.c.payload))]
        job_rows = [str(tuple(r)) for r in connection.execute(sa.select(platform.job_view))]
    assert all(title not in text for text in payloads + job_rows)
    send = ScriptedSend()
    dispatcher(env, send).run_once()
    assert title not in caplog.text
    assert all(title not in message.body for _, message in send.sent)


@pytest.mark.req("EF-INT-006")
def test_dispatch_job_is_idempotent_and_swept_after_a_lost_queue(
    env: NotifyEnv, tmp_path: Path
) -> None:
    (tmp_path / "queues").mkdir()
    pools = JobPools(tmp_path / "queues", engine=env.engine, clock=env.clock, seed="s")
    send = ScriptedSend()
    register_notification_job(pools, dispatcher(env, send))
    env.add_channel()
    with env.engine.begin() as connection:
        env.bus.publish(connection, "backup.failed", {})
    # the process died before request_dispatch: nothing queued. The periodic sweep finds the row.
    env.clock.advance(minutes=6)
    pools.tick()
    huey = pools.huey("io")
    task = huey.dequeue()
    assert task is not None
    huey.execute(task)
    assert len(send.sent) == 1
    assert count(env, pending) == 0
    request_dispatch(pools)
    request_dispatch(pools)  # the same constant key: one job
    assert huey.pending_count() == 1
    with env.engine.connect() as connection:
        keys = [r.dedupe_key for r in connection.execute(sa.select(platform.job_view))]
    assert set(keys) == {"notify:dispatch", "periodic:notify.dispatch:"}


@pytest.mark.req("ENF-SEC-016", "EF-INT-006")
def test_ssrf_is_checked_again_at_send_time_and_a_removed_lan_target_blocks(
    env: NotifyEnv,
) -> None:
    lan = asyncio.run(LanTargets.declare(["nas.lan:80"], env.resolver))
    env.add_channel("nas", "ntfy://nas.lan/topic-secret-path", lan=lan)
    send = ScriptedSend()
    declared = [lan]
    work = NotificationDispatcher(
        env.engine,
        env.vault,
        env.clock,
        send=send,
        bus=env.bus,
        lan_targets=lambda: declared[0],
        resolver=env.resolver,
    )
    with env.engine.begin() as connection:
        env.bus.publish(connection, "disk.low", {})
    declared[0] = LanTargets.empty()  # the admin removed the target
    assert work.run_once().failed == 1
    assert send.sent == []
    with env.engine.connect() as connection:
        assert connection.execute(sa.select(pending.c.attempts)).scalar_one() == 1
    declared[0] = lan
    env.clock.advance(hours=1)
    assert work.run_once().sent == 1


@pytest.mark.req("ENF-SEC-016")
def test_poc_rebinding_name_and_direct_loopback_never_reach_the_server(env: NotifyEnv) -> None:
    import http.server
    import socket
    import threading

    hits: list[str] = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_port
    real = socket.getaddrinfo

    def rebinding(host: str, *args: object, **kwargs: object) -> object:
        return real("127.0.0.1" if host == "rebind.example" else host, *args, **kwargs)  # type: ignore[arg-type]

    socket.getaddrinfo = rebinding  # type: ignore[assignment]
    try:
        rebind = f"ntfy://u:pw@rebind.example:{port}/topic"
        with pytest.raises(ChannelUrlRefused):  # public at check time, loopback at send time
            asyncio.run(validate_channel_url(rebind, lan=LanTargets.empty(), resolver=env.resolver))
        # a URL that was never validated (a row written by hand) is refused at send time too
        direct = f"gotify://127.0.0.1:{port}/tok-secret-path"
        with env.engine.begin() as connection:
            env.store.save(connection, "direct", ValidatedChannel(direct, "gotify", False))
            env.bus.publish(connection, "disk.low", {})
        work = NotificationDispatcher(env.engine, env.vault, env.clock, bus=env.bus)
        assert work.run_once().failed == 1
    finally:
        socket.getaddrinfo = real
        server.shutdown()
        server.server_close()
    assert hits == []


@pytest.mark.req("EF-INT-006")
def test_a_poisoned_row_costs_itself_an_attempt_and_starves_no_other(env: NotifyEnv) -> None:
    env.add_channel()
    with env.engine.begin() as connection:
        env.bus.publish(connection, "backup.failed", {})
        env.bus.publish(connection, "disk.low", {})

    def texts(event_type: str, payload: object) -> dict[str, str]:
        if event_type == "backup.failed":
            raise RuntimeError("cannot resolve")
        return {}

    send = ScriptedSend()
    work = NotificationDispatcher(
        env.engine, env.vault, env.clock, send=send, bus=env.bus, texts=texts
    )
    report = work.run_once()
    assert report.sent == 1
    assert report.failed == 1
    assert len(send.sent) == 1
    for _ in range(10):
        env.clock.advance(hours=2)
        work.run_once()
    assert count(env, pending) == 0
    with env.engine.connect() as connection:
        types = [r.type for r in connection.execute(sa.select(event.c.type))]
    assert types.count("notify.failed") == 1


@pytest.mark.req("EF-INT-006")
def test_lease_is_renewed_per_message_so_no_other_worker_takes_a_row(env: NotifyEnv) -> None:
    env.add_channel()
    with env.engine.begin() as connection:
        for _ in range(3):
            env.bus.publish(connection, "backup.failed", {})
    seen: list[bool] = []

    def slow(url: str, message: Message) -> SendResult:
        with env.engine.connect() as connection:
            soonest = connection.execute(sa.select(sa.func.min(pending.c.next_try_at))).scalar_one()
        seen.append(soonest > env.clock.now())  # a second worker would claim at <= now
        env.clock.advance(minutes=4)
        return SendResult(ok=True, code="notify.sent")

    work = NotificationDispatcher(env.engine, env.vault, env.clock, send=slow, bus=env.bus)
    assert work.run_once().sent == 3
    assert seen == [True, True, True]


@pytest.mark.req("ENF-SEC-015")
def test_enumerated_payload_values_are_per_event_type(env: NotifyEnv) -> None:
    with env.engine.begin() as connection:
        env.bus.publish(connection, "notify.failed", {"reason": "retries_exhausted"})
        for event_type, payload in (
            ("notify.failed", {"reason": "unreachable"}),
            ("disk.low", {"reason": "berserk"}),
            ("disk.low", {"kind": "Berserk"}),
            ("disk.low", {"state": "not_a_state"}),
            ("disk.low", {"code": "free text"}),
        ):
            with pytest.raises(EventPayloadError):
                env.bus.publish(connection, event_type, payload)
        env.bus.publish(connection, "disk.low", {"state": "dead_letter", "code": "disk.low"})
