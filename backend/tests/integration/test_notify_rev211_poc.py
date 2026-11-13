"""Contradicteur #211 (L14): PoC. In production every message leaves through the instance's
channels, whoever it is for (`ChannelDelivery` ignores `user_id`), so whatever leaves for anyone
is read by everyone who reads those channels, a child included. These tests read the whole sink,
not the part addressed to one profile."""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.schema import catalog, identity
from tests.notify_env import MEMBER_ID, Env, world


@pytest.fixture
def shared(settings: Settings) -> Any:
    with world(settings) as built:  # the sink is the one shared channel
        built.at(9)
        yield built


def _channel(ctx: Env) -> str:
    return "\n".join(f"{m.title}\n{m.body}" for _u, m in ctx.sink.sent)


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_poc_adult_followed_title_above_a_child_ceiling_reaches_the_shared_channel(
    shared: Env,
) -> None:
    adult = shared.work("Adult Only Volume", age=18)
    unrated = shared.work("Unrated Volume", age=None)
    shared.series("Series With An Unrated Member", [unrated])
    shared.follow(MEMBER_ID, adult)
    shared.follow(MEMBER_ID, unrated)
    shared.chapter(adult)
    shared.chapter(unrated)
    shared.at(8, day=6)
    shared.run()
    text = _channel(shared)
    assert shared.sink.sent  # the control: the adult's digest did leave
    assert "Adult Only Volume" not in text
    assert "Series With An Unrated Member" not in text


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_poc_adult_household_request_title_reaches_the_shared_channel(shared: Env) -> None:
    adult = shared.work("Adult Request Title", age=18)
    shared.request_arrived(MEMBER_ID, adult, visibility="household")
    shared.at(8, day=6)
    shared.run()
    assert shared.sink.sent
    assert "Adult Request Title" not in _channel(shared)


@pytest.mark.req("EF-INT-021")
def test_poc_a_purged_work_still_sends_a_notice(shared: Env) -> None:
    work = shared.work("Purged Later", age=6)
    shared.follow(MEMBER_ID, work)
    shared.chapter(work)
    with shared.engine.begin() as connection:
        connection.execute(
            sa.delete(identity.user_state).where(identity.user_state.c.work_id == work)
        )
        connection.execute(sa.delete(catalog.edition).where(catalog.edition.c.work_id == work))
        connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == work))
    shared.at(8, day=6)
    shared.run()
    assert shared.sink.sent == []  # nothing left to tell
