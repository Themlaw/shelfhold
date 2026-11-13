"""EF-INT-021: per-person notifications, from the event to the message that leaves.

The router and the sender are the production ones, over a migrated database and a real bus; only
the last hop (the channel) is a recording sink. The clock is a value moved by hand."""

from __future__ import annotations

import json
from typing import Any

import pytest

from shelfhold.config import Settings
from tests.notify_env import (
    ADMIN_ID,
    CHILD_ID,
    MEMBER_ID,
    OTHER_ID,
    SIBLING_ID,
    Env,
    Sink,
    world,
)


@pytest.fixture
def ctx(settings: Settings) -> Any:
    with world(settings) as built:
        built.at(9)  # 09:00 UTC, after the digest hour of the day
        yield built


# -- the default is a daily digest; only two things leave at once ------------------------------


@pytest.mark.req("EF-INT-021")
def test_daily_digest_default_immediate_only_for_request_and_outage(ctx: Env) -> None:
    work = ctx.work("Ocean Tales")
    ctx.follow(MEMBER_ID, work)
    ctx.chapter(work)  # a chapter: digest
    ctx.run()
    assert ctx.sink.sent == []  # nothing leaves at 09:00 for a digest due tomorrow at 08:00
    ctx.request_arrived(MEMBER_ID, work, visibility="household")  # a request arrival: at once
    ctx.run()
    assert [u for u, _ in ctx.sink.sent] == [MEMBER_ID]
    assert "arrived" in ctx.sink.to(MEMBER_ID)[0].title.lower()
    assert "Ocean Tales" not in ctx.sink.channel()  # no title in a shared channel
    ctx.at(8, day=6)
    ctx.run()
    assert len(ctx.sink.to(MEMBER_ID)) == 2  # the digest, at 08:00 the next day
    assert "1 new chapter" in ctx.sink.to(MEMBER_ID)[1].body
    assert "Ocean Tales" not in ctx.sink.channel()


@pytest.mark.req("EF-INT-021")
def test_thirty_chapters_one_grouped_digest(ctx: Env) -> None:
    works = [ctx.work(f"Chapter {i}") for i in range(30)]
    series = ctx.series("Grouped Saga", works)
    for work in works:
        ctx.follow(MEMBER_ID, work)
        ctx.chapter(work)
    ctx.at(8, day=6)
    ctx.run()
    messages = ctx.sink.to(MEMBER_ID)
    assert len(messages) == 1  # one message, not thirty
    assert "30 new chapter" in messages[0].body
    assert "Grouped Saga" not in messages[0].body  # no series name in a shared channel
    assert series not in messages[0].body  # an identifier is not text for a person
    assert ctx.pending() == []  # and it is gone once sent


@pytest.mark.req("EF-INT-021")
def test_a_digest_is_sent_once_and_an_empty_day_sends_nothing(ctx: Env) -> None:
    work = ctx.work("Once")
    ctx.follow(MEMBER_ID, work)
    ctx.chapter(work)
    ctx.at(8, day=6)
    ctx.run()
    ctx.run()
    assert len(ctx.sink.to(MEMBER_ID)) == 1
    ctx.at(8, day=7)
    ctx.run()
    assert len(ctx.sink.to(MEMBER_ID)) == 1


@pytest.mark.req("EF-INT-021")
def test_digest_hour_is_the_local_time_of_each_profile(ctx: Env) -> None:
    work = ctx.work("Zones")
    ctx.follow(MEMBER_ID, work)
    ctx.follow(OTHER_ID, work)
    ctx.profile(MEMBER_ID, timezone="Europe/Paris")  # 08:00 there is 07:00 UTC
    ctx.profile(OTHER_ID, timezone="Asia/Tokyo")  # 08:00 there is 23:00 UTC the day before
    ctx.at(22, day=5)
    ctx.chapter(work)
    ctx.at(22, minute=30, day=5)
    ctx.run()
    assert ctx.sink.to(OTHER_ID) == [] and ctx.sink.to(MEMBER_ID) == []
    ctx.at(23, day=5)  # Tokyo's morning
    ctx.run()
    assert len(ctx.sink.to(OTHER_ID)) == 1 and ctx.sink.to(MEMBER_ID) == []
    ctx.at(7, day=6)  # Paris's morning
    ctx.run()
    assert len(ctx.sink.to(MEMBER_ID)) == 1 and len(ctx.sink.to(OTHER_ID)) == 1


@pytest.mark.req("EF-INT-021")
def test_a_profile_in_immediate_mode_gets_chapters_at_once(ctx: Env) -> None:
    work = ctx.work("Fast")
    ctx.follow(MEMBER_ID, work)
    ctx.follow(OTHER_ID, work)
    ctx.profile(MEMBER_ID, notify_mode="immediate")
    ctx.chapter(work)
    ctx.run()
    assert len(ctx.sink.to(MEMBER_ID)) == 1
    assert ctx.sink.to(OTHER_ID) == []  # the other one kept the default


# -- quiet hours -------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-021")
def test_quiet_hours_delay_non_urgent(ctx: Env) -> None:
    work = ctx.work("Night Owl")
    ctx.follow(MEMBER_ID, work)
    ctx.profile(MEMBER_ID, quiet_start="06:00", quiet_end="10:00")  # covers 08:00
    ctx.at(23, day=5)
    ctx.chapter(work)
    ctx.at(8, day=6)
    ctx.run()
    assert ctx.sink.to(MEMBER_ID) == []  # the digest hour is inside the quiet hours
    ctx.at(9, minute=59, day=6)
    ctx.run()
    assert ctx.sink.to(MEMBER_ID) == []
    ctx.at(10, day=6)
    ctx.run()
    assert len(ctx.sink.to(MEMBER_ID)) == 1  # at the end of the quiet hours, not a minute later


@pytest.mark.req("EF-INT-021")
def test_default_quiet_hours_hold_a_request_arrival_until_morning(ctx: Env) -> None:
    work = ctx.work("Late Arrival")
    ctx.at(23, day=5)  # in the default quiet hours (21:00 to 07:00)
    ctx.request_arrived(MEMBER_ID, work, visibility="household")
    ctx.run()
    assert ctx.sink.to(MEMBER_ID) == []
    ctx.at(7, day=6)
    ctx.run()
    assert len(ctx.sink.to(MEMBER_ID)) == 1


# -- outages -----------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-021")
def test_outage_over_one_hour_is_immediate_for_the_admin_even_at_night(ctx: Env) -> None:
    ctx.at(2, day=6)  # quiet hours: an outage is the exception
    ctx.outage()
    ctx.run()
    assert ctx.sink.sent == []  # not an hour yet
    ctx.advance(minutes=30)
    ctx.outage()  # still down (an outage is a run of the same event)
    ctx.run()
    assert ctx.sink.sent == []
    ctx.advance(minutes=31)
    ctx.outage()
    ctx.run()
    assert [u for u, _ in ctx.sink.sent] == [ADMIN_ID]
    ctx.advance(minutes=30)
    ctx.outage()
    ctx.run()
    assert len(ctx.sink.sent) == 1  # told once per outage, not at every failure


@pytest.mark.req("EF-INT-021")
def test_outage_is_told_once_even_when_no_new_event_comes(ctx: Env) -> None:
    """The sweep, not a new event, notices that an hour has gone by."""
    ctx.outage()
    ctx.advance(minutes=45)
    ctx.outage()
    ctx.advance(minutes=20)  # 65 minutes after the first, 20 after the last: still the same outage
    ctx.run()
    assert [u for u, _ in ctx.sink.sent] == [ADMIN_ID]


@pytest.mark.req("EF-INT-021")
def test_a_new_outage_after_a_recovery_is_told_again(ctx: Env) -> None:
    ctx.outage()
    ctx.advance(minutes=70)
    ctx.outage()
    ctx.run()
    ctx.advance(hours=5)  # nothing failed for five hours: it recovered
    ctx.outage()
    ctx.advance(minutes=65)
    ctx.outage()
    ctx.run()
    assert len(ctx.sink.to(ADMIN_ID)) == 2


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_outage_never_reaches_a_member_or_a_child(ctx: Env) -> None:
    for _ in range(3):
        ctx.outage()
        ctx.advance(minutes=40)
    ctx.run()
    assert [u for u, _ in ctx.sink.sent] == [ADMIN_ID]
    for other in (MEMBER_ID, OTHER_ID, CHILD_ID, SIBLING_ID):
        ctx.profile(other, notify_mode="immediate")
    for _ in range(3):
        ctx.outage("source.broken")
        ctx.advance(minutes=40)
    ctx.run()
    assert {u for u, _ in ctx.sink.sent} == {ADMIN_ID}


# -- the reminder of a withdrawal waiting for an admin -----------------------------------------


@pytest.mark.req("EF-INT-021", "EF-BIB-037")
def test_withdrawal_pending_reminds_daily_never_applies(ctx: Env) -> None:
    ctx.withdrawals[0] = 2  # two files wait to leave a family shelf
    ctx.at(9, day=5)
    ctx.run()
    assert ctx.sink.sent == []  # the reminder is part of tomorrow's digest
    for day in (6, 7, 8):
        ctx.at(8, day=day)
        ctx.run()
        ctx.at(9, day=day)
        ctx.run()  # a second sweep the same day changes nothing
    admin = ctx.sink.to(ADMIN_ID)
    assert len(admin) == 3  # once a day
    assert all("withdrawal" in m.body.lower() for m in admin)
    assert all(u == ADMIN_ID for u, _ in ctx.sink.sent)  # the admin only: no member, no child
    ctx.withdrawals[0] = 0  # the admin took them out
    ctx.at(8, day=9)
    ctx.run()
    ctx.at(8, day=10)
    ctx.run()
    assert len(ctx.sink.to(ADMIN_ID)) == 3  # no more reminders: and no empty message either


@pytest.mark.req("EF-INT-021", "EF-BIB-037")
def test_the_reminder_only_reads_it_has_no_door_to_apply_a_withdrawal(ctx: Env) -> None:
    import shelfhold.integrations.notify as notify
    import shelfhold.integrations.notify.service as service

    names = {*dir(notify), *dir(service)}
    allowed = {"PendingWithdrawals", "pending_withdrawals"}
    assert not {n for n in names - allowed if "withdraw" in n.lower() or "apply" in n.lower()}
    ctx.withdrawals[0] = 1
    ctx.at(9, day=5)
    ctx.run()
    ctx.at(8, day=6)
    ctx.run()
    assert len(ctx.sink.to(ADMIN_ID)) == 1 and ctx.withdrawals == [1]  # read, never changed


# -- the attacks: who may be told about what ---------------------------------------------------


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_child_is_told_nothing_of_an_unrated_work_she_follows(ctx: Env) -> None:
    hidden = ctx.work("Hidden Unrated", age=None)
    visible = ctx.work("Fine For Kids", age=6)
    ctx.follow(CHILD_ID, hidden)  # a follow row she should not have, or one made before a rerate
    ctx.follow(CHILD_ID, visible)
    ctx.profile(CHILD_ID, notify_mode="immediate")
    ctx.chapter(hidden)
    ctx.chapter(visible)
    ctx.run()
    text = ctx.sink.channel()
    assert "1 new chapter" in ctx.sink.to(CHILD_ID)[0].body  # only the visible one is counted
    assert "Fine For Kids" not in text and "Hidden Unrated" not in text and hidden not in text


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_child_is_told_nothing_of_a_work_above_her_ceiling(ctx: Env) -> None:
    above = ctx.work("Too Old For Her", age=16)  # her ceiling is 6
    ctx.follow(CHILD_ID, above)
    ctx.follow(SIBLING_ID, above)  # a ceiling of 10: also above
    ctx.follow(MEMBER_ID, above)
    for kid in (CHILD_ID, SIBLING_ID):
        ctx.profile(kid, notify_mode="immediate")
    ctx.chapter(above)
    ctx.request_arrived(CHILD_ID, above, visibility="household")
    ctx.at(8, day=6)
    ctx.run()
    assert ctx.sink.to(CHILD_ID) == [] and ctx.sink.to(SIBLING_ID) == []
    assert "2 new" not in ctx.sink.text(MEMBER_ID)
    assert "1 new chapter" in ctx.sink.text(MEMBER_ID)  # the adult is not narrowed
    assert "Too Old For Her" not in ctx.sink.channel()


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_series_grouping_does_not_carry_a_hidden_title_to_a_child(ctx: Env) -> None:
    """A series whose member is hidden from her is hidden as a whole: its name, its count, and
    the visible chapter that would have lent it a cover."""
    shown = ctx.work("Shown Chapter", age=6)
    hidden = ctx.work("Spoiler Chapter", age=None)
    ctx.series("Series Holding A Hidden Member", [shown, hidden])
    ctx.follow(CHILD_ID, shown)
    plain = ctx.work("Plain Kids Book", age=6)
    ctx.series("Fully Visible Series", [plain])
    ctx.follow(CHILD_ID, plain)
    ctx.profile(CHILD_ID, notify_mode="immediate")
    ctx.chapter(shown)
    ctx.chapter(plain)
    ctx.run()
    text = ctx.sink.channel()
    # the control: she is told of what is hers (one chapter, the plain series), not the other
    assert len(ctx.sink.to(CHILD_ID)) == 1 and "1 new chapter" in ctx.sink.to(CHILD_ID)[0].body
    for hidden_name in ("Series Holding A Hidden Member", "Shown Chapter", "Spoiler Chapter"):
        assert hidden_name not in text


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_a_work_rerated_after_the_event_is_not_sent_to_the_child(ctx: Env) -> None:
    """The decision is taken again when the message is built, not only when the row is queued."""
    work = ctx.work("Rerated Later", age=6)
    ctx.follow(CHILD_ID, work)
    ctx.chapter(work)
    assert ctx.pending() != []  # it was hers when it came
    other = ctx.work("Still Hers", age=6)
    ctx.follow(CHILD_ID, other)
    ctx.chapter(other)
    ctx.rate(work, 16)  # the admin rated it up
    ctx.at(8, day=6)
    ctx.run()
    assert "1 new chapter" in ctx.sink.text(CHILD_ID)  # the control: the digest leaves, one line
    assert "Rerated Later" not in ctx.sink.channel()
    assert ctx.pending() == []  # dropped, not kept for later


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_a_ceiling_lowered_after_the_event_stops_the_message(ctx: Env) -> None:
    work = ctx.work("Ten Plus", age=10)
    ctx.follow(SIBLING_ID, work)  # a ceiling of 10
    ctx.chapter(work)
    low = ctx.work("Six Plus", age=6)
    ctx.follow(SIBLING_ID, low)
    ctx.chapter(low)
    ctx.profile(SIBLING_ID, max_age_rating=6)
    ctx.at(8, day=6)
    ctx.run()
    assert "1 new chapter" in ctx.sink.text(SIBLING_ID)  # the control: only "Six Plus" counts
    assert "Ten Plus" not in ctx.sink.channel()


@pytest.mark.req("EF-INT-021", "EF-USR-016")
def test_a_request_arrival_goes_to_the_requester_alone(ctx: Env) -> None:
    work = ctx.work("Private Wish")
    for follower in (OTHER_ID, ADMIN_ID, MEMBER_ID):
        ctx.follow(follower, work)
    ctx.request_arrived(MEMBER_ID, work, visibility="private")
    ctx.run()
    assert [u for u, _ in ctx.sink.sent] == [MEMBER_ID]
    assert "1 request(s) arrived" in ctx.sink.text(MEMBER_ID)
    assert "For member" in ctx.sink.text(MEMBER_ID)  # attributable, with no title
    assert "Private Wish" not in ctx.sink.channel()


@pytest.mark.req("EF-INT-021", "EF-USR-016")
def test_a_private_title_is_never_in_another_persons_digest(ctx: Env) -> None:
    """A chapter that a member's private request brought is theirs: the other follower hears
    nothing, and the admin, who may oversee, is told without the title (it is masked)."""
    work = ctx.work("Quiet Reading")
    for follower in (OTHER_ID, ADMIN_ID, MEMBER_ID):
        ctx.follow(follower, work)
    ctx.chapter(work, actor=MEMBER_ID, visibility="private")
    ctx.at(8, day=6)
    ctx.run()
    assert ctx.sink.to(OTHER_ID) == []
    assert "1 new chapter" in ctx.sink.text(MEMBER_ID)
    assert "Quiet Reading" not in ctx.sink.channel()  # no title in a shared channel at all


@pytest.mark.req("EF-INT-021", "EF-USR-016")
def test_a_child_request_arrival_goes_to_the_requester_only(ctx: Env) -> None:
    work = ctx.work("Kids Pick", age=6)
    ctx.follow(SIBLING_ID, work)
    ctx.profile(SIBLING_ID, notify_mode="immediate")
    ctx.request_arrived(CHILD_ID, work, visibility="household")
    ctx.run()
    assert [u for u, _ in ctx.sink.sent] == [CHILD_ID]  # the requester, and no one else
    assert "1 request(s) arrived" in ctx.sink.text(CHILD_ID)
    assert "Kids Pick" not in ctx.sink.channel()
    assert "kid" not in ctx.sink.to(CHILD_ID)[0].body.lower()  # a child is never named


@pytest.mark.req("EF-INT-021")
def test_a_user_who_is_gone_is_told_nothing(ctx: Env) -> None:
    work = ctx.work("Gone")
    ctx.follow(MEMBER_ID, work)
    ctx.chapter(work)
    with ctx.engine.begin() as connection:
        connection.exec_driver_sql('DELETE FROM "user" WHERE id = ?', (MEMBER_ID,))
    ctx.at(8, day=6)
    ctx.run()
    assert ctx.sink.sent == []
    assert ctx.pending() == []


# -- what is stored and what is logged ---------------------------------------------------------


@pytest.mark.req("EF-INT-021", "ENF-SEC-015")
def test_the_queue_holds_identifiers_and_counts_never_a_title(ctx: Env) -> None:
    work = ctx.work("A Very Distinctive Title")
    series = ctx.series("A Very Distinctive Series", [work])
    ctx.follow(MEMBER_ID, work)
    ctx.chapter(work)
    rows = ctx.pending()
    assert rows
    stored = json.dumps([r.payload for r in rows])
    assert "Distinctive" not in stored
    assert series in stored or work in stored


@pytest.mark.req("EF-INT-021", "ENF-SEC-016")
def test_a_failed_send_keeps_the_row_then_drops_it_without_logging_the_text(
    settings: Settings,
) -> None:
    from structlog.testing import capture_logs

    sink = Sink(ok=False)
    with world(settings, sink=sink) as ctx, capture_logs() as logs:
        ctx.at(9)
        work = ctx.work("Secret Plot Title")
        ctx.follow(MEMBER_ID, work)
        ctx.profile(MEMBER_ID, notify_mode="immediate")
        ctx.chapter(work)
        ctx.run()
        assert len(ctx.pending()) == 1  # kept for another try
        for _ in range(8):
            ctx.advance(hours=1)
            ctx.run()
        assert ctx.pending() == []  # gave up after a few tries
        sink.ok = True
        ctx.run()
        assert sink.sent == []  # and does not come back
    assert any(entry["event"].startswith("notify.") for entry in logs)  # it did say something
    assert "Secret Plot Title" not in str(logs)


@pytest.mark.req("EF-INT-021")
def test_a_work_purged_between_the_event_and_the_digest_sends_nothing(ctx: Env) -> None:
    import sqlalchemy as sa

    from shelfhold.infrastructure.schema import catalog, identity

    work = ctx.work("Purged Soon", age=6)
    other = ctx.work("Kept", age=6)
    for w in (work, other):
        ctx.follow(MEMBER_ID, w)
        ctx.chapter(w)
    with ctx.engine.begin() as connection:
        connection.execute(
            sa.delete(identity.user_state).where(identity.user_state.c.work_id == work)
        )
        connection.execute(sa.delete(catalog.edition).where(catalog.edition.c.work_id == work))
        connection.execute(sa.delete(catalog.work).where(catalog.work.c.id == work))
    ctx.at(8, day=6)
    ctx.run()
    assert "1 new chapter" in ctx.sink.text(MEMBER_ID)  # only the one that still exists
