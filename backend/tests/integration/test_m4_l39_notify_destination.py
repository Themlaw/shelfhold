"""M4 L39: a destination of one's own for the notifications of an adult, and `notify_mode` read.

The channels of the instance are read by everyone (a child included), so a message that leaves
through them carries no title. An adult may seal a destination of their own (an Apprise URL):
the titles go there and **only** there, never to the shared channels. A child has no destination
of her own, whoever asks; and a child is never told at once, whatever her `notify_mode` says.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from tests.notify_env import (
    ADMIN_ID,
    CHILD_ID,
    MEMBER_ID,
    OTHER_ID,
    Env,
    PrivateSink,
    world,
)

TITLE = "Ocean Tales"


@pytest.fixture
def ctx(settings: Settings) -> Env:  # type: ignore[misc]
    private = PrivateSink(users={MEMBER_ID})
    with world(settings, private=private) as built:
        built.at(9)
        yield built  # type: ignore[misc]


def _follow_and_store(ctx: Env, *people: str) -> str:
    work = ctx.work(TITLE)
    for person in people:
        ctx.follow(person, work)
    ctx.chapter(work)
    return work


@pytest.mark.req("EF-INT-021", "EF-USR-006", "ENF-SEC-016")
def test_titles_only_to_the_profile_destination(ctx: Env) -> None:
    """The adult with a destination reads the title there and nowhere else; the shared channels
    carry counts only, for the others, and nothing at all for the one served privately."""
    _follow_and_store(ctx, MEMBER_ID, OTHER_ID, CHILD_ID)
    ctx.at(8, day=6)
    ctx.run()
    assert ctx.private is not None
    assert TITLE in ctx.private.text(MEMBER_ID)
    assert {u for u, _ in ctx.private.sent} == {MEMBER_ID}  # nobody else is served there
    assert TITLE not in ctx.sink.channel()  # the shared channels never carry a title
    assert ctx.sink.to(MEMBER_ID) == []  # and nothing twice: the destination is where they read
    assert "1 new chapter" in ctx.sink.text(OTHER_ID)  # an adult with none: counts, shared
    assert "1 new chapter" in ctx.sink.text(CHILD_ID)
    assert TITLE not in ctx.private.text(OTHER_ID) + ctx.private.text(CHILD_ID)


@pytest.mark.req("EF-INT-021", "EF-USR-006", "ENF-SEC-016")
def test_a_failing_destination_never_falls_back_to_the_shared_channels(ctx: Env) -> None:
    _follow_and_store(ctx, MEMBER_ID)
    assert ctx.private is not None
    ctx.private.ok = False
    ctx.at(8, day=6)
    ctx.run()
    assert ctx.sink.sent == [] and ctx.private.sent == []
    assert len(ctx.pending()) == 1  # kept for a new attempt, like any failed send
    assert TITLE not in ctx.sink.channel()


@pytest.mark.req("EF-INT-021", "EF-USR-006", "EF-USR-016")
def test_a_child_has_no_destination_of_her_own(ctx: Env) -> None:
    """Even a destination written to the row of a child (by hand, a restore) is never used: her
    notices go through the shared channels, without a title."""
    assert ctx.private is not None
    ctx.private.users.add(CHILD_ID)
    _follow_and_store(ctx, CHILD_ID)
    ctx.at(8, day=6)
    ctx.run()
    assert ctx.private.to(CHILD_ID) == []
    assert "1 new chapter" in ctx.sink.text(CHILD_ID)
    assert TITLE not in ctx.sink.channel() + ctx.private.text(CHILD_ID)


@pytest.mark.req("EF-INT-021", "EF-USR-016")
def test_notify_mode_is_read(ctx: Env) -> None:
    """`immediate` leaves at once for an adult who chose it; `digest` waits for 08:00."""
    work = ctx.work(TITLE)
    ctx.follow(OTHER_ID, work)
    ctx.follow(ADMIN_ID, work)
    ctx.profile(OTHER_ID, notify_mode="immediate")
    ctx.profile(ADMIN_ID, notify_mode="digest")
    ctx.chapter(work)
    ctx.run()  # 09:00
    assert [u for u, _ in ctx.sink.sent] == [OTHER_ID]
    ctx.at(8, day=6)
    ctx.run()
    assert ADMIN_ID in {u for u, _ in ctx.sink.sent}


@pytest.mark.req("EF-INT-021", "EF-USR-016")
def test_a_child_is_never_told_at_once(ctx: Env) -> None:
    """Whatever her `notify_mode` says, and for the arrival of her own request too: a digest."""
    work = ctx.work(TITLE)
    ctx.follow(CHILD_ID, work)
    ctx.profile(CHILD_ID, notify_mode="immediate")
    ctx.chapter(work)
    ctx.request_arrived(CHILD_ID, work, visibility="household")
    ctx.run()  # 09:00
    assert ctx.sink.to(CHILD_ID) == []
    ctx.at(8, day=6)
    ctx.run()
    assert len(ctx.sink.to(CHILD_ID)) == 1  # one digest, the next morning
    with ctx.engine.connect() as connection:
        mode = connection.execute(
            sa.text("SELECT notify_mode FROM user WHERE id = :u"), {"u": CHILD_ID}
        ).scalar_one()
    assert mode == "immediate"  # read, not rewritten: the rule is applied where it is read
