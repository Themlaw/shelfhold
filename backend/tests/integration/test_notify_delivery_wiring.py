"""EF-INT-021, ENF-SEC-015, ENF-SEC-016: the way out, and the wiring as the application composes it.

What a per-person message may carry through the instance's shared channels; that no URL and no
text reach a log line; that the outbound check of the operating notifications holds here too; and
that the real application subscribes the router and registers the sweep (a draft that was never
connected would pass every other test of this lot)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from structlog.testing import capture_logs

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import identity, platform
from shelfhold.integrations.apprise import Message, ValidatedChannel
from shelfhold.integrations.apprise.send import SendResult
from shelfhold.integrations.notify import ChannelDelivery
from tests.auth_env import AuthEnv
from tests.federated_env import MEMBER_ID, FakeSource, instance
from tests.notifications_env import CHANNEL_URL, TOKEN, NotifyEnv, ScriptedSend, notify_env
from tests.notify_env import ADMIN_ID, CHILD_ID, OTHER_ID, world

MESSAGE = Message("Shelfhold - daily digest", "- A Title: 3 new chapter(s)")


@pytest.fixture
def channels(settings: Settings) -> Iterator[NotifyEnv]:
    with notify_env(settings) as built:
        yield built


def delivery(env: NotifyEnv, send: ScriptedSend) -> ChannelDelivery:
    return ChannelDelivery(env.engine, env.vault, env.clock, send=send, resolver=env.resolver)


# -- the shared channel ------------------------------------------------------------------------


@pytest.mark.req("EF-INT-021", "EF-USR-016", "EF-USR-006")
def test_a_shared_channel_never_carries_a_title_for_anyone(settings: Settings) -> None:
    """Whoever the message is for, the channel is read by all: no request title, series name or
    Work title, private, household or adult."""
    with world(settings) as ctx:
        ctx.at(9)
        work = ctx.work("Whispered Title")
        household = ctx.work("Family Title")
        adult = ctx.work("Adult Title", age=18)
        series = ctx.series("A Series Name", [adult])
        ctx.follow(MEMBER_ID, adult)
        ctx.chapter(adult)
        ctx.request_arrived(MEMBER_ID, work, visibility="private")
        ctx.request_arrived(MEMBER_ID, household, visibility="household")
        ctx.at(8, day=6)
        ctx.run()
        text = ctx.sink.channel()
        assert ctx.sink.sent
        for name in ("Whispered Title", "Family Title", "Adult Title", "A Series Name", series):
            assert name not in text


@pytest.mark.req("EF-INT-021")
def test_a_message_names_the_profile_unless_it_is_a_child_and_may_link_the_app(
    settings: Settings,
) -> None:
    with world(settings) as ctx:
        ctx.at(9)
        for who in (MEMBER_ID, CHILD_ID):
            work = ctx.work(f"For {who}", age=6)
            ctx.request_arrived(who, work, visibility="household")
        ctx.run()
        assert "For member" in ctx.sink.text(MEMBER_ID)
        child = ctx.sink.text(CHILD_ID)
        assert "kid" not in child and "For " not in child  # anonymous
        assert "arrived" in child


@pytest.mark.req("EF-INT-021", "ENF-SEC-015")
def test_a_profile_name_cannot_make_the_message_a_live_link_or_mention(settings: Settings) -> None:
    with world(settings) as ctx:
        ctx.at(9)
        ctx.profile(OTHER_ID, username="@everyone")
        work = ctx.work("W", age=6)
        ctx.request_arrived(OTHER_ID, work, visibility="household")
        ctx.run()
        assert "`@everyone`" in ctx.sink.to(OTHER_ID)[0].body


# -- ChannelDelivery ---------------------------------------------------------------------------


@pytest.mark.req("EF-INT-021", "ENF-SEC-016")
def test_delivery_sends_through_every_configured_channel_after_the_check(
    channels: NotifyEnv,
) -> None:
    channels.add_channel("ops")
    send = ScriptedSend()
    assert delivery(channels, send)(ADMIN_ID, MESSAGE) is True
    assert [m for _, m in send.sent] == [MESSAGE]
    send.ok = False
    assert delivery(channels, send)(ADMIN_ID, MESSAGE) is False  # the row will be retried


@pytest.mark.req("EF-INT-021")
def test_delivery_with_no_channel_has_nothing_to_wait_for(channels: NotifyEnv) -> None:
    send = ScriptedSend()
    assert delivery(channels, send)(ADMIN_ID, MESSAGE) is True
    assert send.sent == []


@pytest.mark.req("EF-INT-021", "ENF-SEC-016")
def test_delivery_checks_the_destination_again_at_every_send(channels: NotifyEnv) -> None:
    direct = "gotify://127.0.0.1:9/never-reached-path"  # loopback: not an allowed destination
    with channels.engine.begin() as connection:
        channels.store.save(connection, "direct", ValidatedChannel(direct, "gotify", False))
    send = ScriptedSend()
    assert delivery(channels, send)(ADMIN_ID, MESSAGE) is False
    assert send.sent == []  # refused before anything left


@pytest.mark.req("EF-INT-021", "ENF-SEC-016")
def test_a_failing_send_logs_neither_the_url_nor_the_text(channels: NotifyEnv) -> None:
    channels.add_channel("ops")

    def exploding(url: str, message: Message) -> SendResult:
        raise RuntimeError(f"cannot reach {url} with {message.body}")

    work = ChannelDelivery(
        channels.engine, channels.vault, channels.clock, send=exploding, resolver=channels.resolver
    )
    with capture_logs() as logs:
        assert work(ADMIN_ID, MESSAGE) is False
    rendered = str(logs)
    assert logs and TOKEN not in rendered and CHANNEL_URL not in rendered
    assert "A Title" not in rendered


# -- the application as composed ---------------------------------------------------------------


@pytest.fixture
def composed(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """The composition `create_app` hands to `subscribe_notifications`: the one the routes use."""
    import shelfhold.main as main_module

    seen: list[Any] = []
    real = main_module.subscribe_notifications

    def spy(comp: Any) -> Any:
        seen.append(comp)
        return real(comp)

    monkeypatch.setattr(main_module, "subscribe_notifications", spy)
    return seen


@pytest.fixture
def api(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, composed: list[Any]
) -> Iterator[AuthEnv]:
    with instance(monkeypatch, settings, FakeSource("alpha", name="Alpha")) as env:
        yield env


def _rows(api: AuthEnv) -> list[Any]:
    engine = sa.create_engine(f"sqlite:///{api.settings.database_path}")
    try:
        with engine.connect() as connection:
            return list(connection.execute(sa.select(identity.notification_digest)))
    finally:
        engine.dispose()


@pytest.mark.req("EF-INT-021")
def test_the_composed_application_routes_a_request_arrival(
    api: AuthEnv, composed: list[Any]
) -> None:
    """The bus the routes use is the one the router listens to: an arrival published there queues
    a notice for the requester, with no test double in between."""
    from shelfhold.infrastructure.ids import new_ulid
    from tests.integration.test_schema_catalog import add_edition, add_work

    assert len(composed) == 1  # the application subscribed the router, once
    composition = composed[0]
    engine = composition.engine
    request_id, work_id = new_ulid(), None
    with engine.begin() as connection:
        work_id = add_work(connection, age_rating=6, rated_at=composition.clock.now())
        add_edition(connection, work_id)
        connection.execute(
            sa.insert(identity.request).values(
                id=request_id, requester_user_id=MEMBER_ID, target_kind="work_id",
                target_value=work_id, state="arrived", visibility="household", work_id=work_id,
            )
        )  # fmt: skip
        published = composition.bus.publish(
            connection, "ops.request_arrived", {"request_id": request_id},
            actor_id=MEMBER_ID, visibility="household",
        )  # fmt: skip
    composition.bus.deliver([published])
    assert {r.user_id for r in _rows(api)} == {MEMBER_ID}


@pytest.mark.req("EF-INT-021")
def test_the_runtime_registers_the_sweep_every_five_minutes(tmp_path: Path) -> None:
    """Through the production registration, on the real job pools: the kind exists, its handler
    runs the sweep it was given, and it is scheduled."""
    from shelfhold.runtime_jobs import register_runtime_jobs
    from tests.maintenance_env import env as job_env

    with job_env(tmp_path) as e:
        register_runtime_jobs(e.pools, MagicMock())
        assert "notify.digest" in e.pools._handlers and "notify.dispatch" in e.pools._handlers
        with e.engine.connect() as connection:
            crons = dict(
                connection.execute(
                    sa.select(platform.schedule.c.job_kind, platform.schedule.c.cron)
                ).all()
            )
        assert crons["notify.digest"] == "*/5 * * * *"
