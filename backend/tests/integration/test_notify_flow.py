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
def env(settings: Settings) -> Any:
    with world(settings) as built:
        built.at(9)  # 09:00 UTC, after the digest hour of the day
        yield built


# -- the default is a daily digest; only two things leave at once ------------------------------


@pytest.mark.req("EF-INT-021")
def test_daily_digest_default_immediate_only_for_request_and_outage(env: Env) -> None:
    work = env.work("Ocean Tales")
    env.follow(MEMBER_ID, work)
    env.chapter(work)  # a chapter: digest
    env.run()
    assert env.sink.sent == []  # nothing leaves at 09:00 for a digest due tomorrow at 08:00
    env.request_arrived(MEMBER_ID, work, visibility="household")  # a request arrival: at once
    env.run()
    assert [u for u, _ in env.sink.sent] == [MEMBER_ID]
    assert "arrived" in env.sink.to(MEMBER_ID)[0].title.lower()
    env.at(8, day=6)
    env.run()
    assert len(env.sink.to(MEMBER_ID)) == 2  # the digest, at 08:00 the next day
    assert "Ocean Tales" in env.sink.to(MEMBER_ID)[1].body


@pytest.mark.req("EF-INT-021")
def test_thirty_chapters_one_grouped_digest(env: Env) -> None:
    works = [env.work(f"Chapter {i}") for i in range(30)]
    series = env.series("Grouped Saga", works)
    for work in works:
        env.follow(MEMBER_ID, work)
        env.chapter(work)
    env.at(8, day=6)
    env.run()
    messages = env.sink.to(MEMBER_ID)
    assert len(messages) == 1  # one message, not thirty
    assert messages[0].body.count("Grouped Saga") == 1
    assert "30" in messages[0].body
    assert series not in messages[0].body  # an identifier is not text for a person
    assert env.pending() == []  # and it is gone once sent


@pytest.mark.req("EF-INT-021")
def test_a_digest_is_sent_once_and_an_empty_day_sends_nothing(env: Env) -> None:
    work = env.work("Once")
    env.follow(MEMBER_ID, work)
    env.chapter(work)
    env.at(8, day=6)
    env.run()
    env.run()
    assert len(env.sink.to(MEMBER_ID)) == 1
    env.at(8, day=7)
    env.run()
    assert len(env.sink.to(MEMBER_ID)) == 1


@pytest.mark.req("EF-INT-021")
def test_digest_hour_is_the_local_time_of_each_profile(env: Env) -> None:
    work = env.work("Zones")
    env.follow(MEMBER_ID, work)
    env.follow(OTHER_ID, work)
    env.profile(MEMBER_ID, timezone="Europe/Paris")  # 08:00 there is 07:00 UTC
    env.profile(OTHER_ID, timezone="Asia/Tokyo")  # 08:00 there is 23:00 UTC the day before
    env.at(22, day=5)
    env.chapter(work)
    env.at(22, minute=30, day=5)
    env.run()
    assert env.sink.to(OTHER_ID) == [] and env.sink.to(MEMBER_ID) == []
    env.at(23, day=5)  # Tokyo's morning
    env.run()
    assert len(env.sink.to(OTHER_ID)) == 1 and env.sink.to(MEMBER_ID) == []
    env.at(7, day=6)  # Paris's morning
    env.run()
    assert len(env.sink.to(MEMBER_ID)) == 1 and len(env.sink.to(OTHER_ID)) == 1


@pytest.mark.req("EF-INT-021")
def test_a_profile_in_immediate_mode_gets_chapters_at_once(env: Env) -> None:
    work = env.work("Fast")
    env.follow(MEMBER_ID, work)
    env.follow(OTHER_ID, work)
    env.profile(MEMBER_ID, notify_mode="immediate")
    env.chapter(work)
    env.run()
    assert len(env.sink.to(MEMBER_ID)) == 1
    assert env.sink.to(OTHER_ID) == []  # the other one kept the default


# -- quiet hours -------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-021")
def test_quiet_hours_delay_non_urgent(env: Env) -> None:
    work = env.work("Night Owl")
    env.follow(MEMBER_ID, work)
    env.profile(MEMBER_ID, quiet_start="06:00", quiet_end="10:00")  # covers 08:00
    env.at(23, day=5)
    env.chapter(work)
    env.at(8, day=6)
    env.run()
    assert env.sink.to(MEMBER_ID) == []  # the digest hour is inside the quiet hours
    env.at(9, minute=59, day=6)
    env.run()
    assert env.sink.to(MEMBER_ID) == []
    env.at(10, day=6)
    env.run()
    assert len(env.sink.to(MEMBER_ID)) == 1  # at the end of the quiet hours, not a minute later


@pytest.mark.req("EF-INT-021")
def test_default_quiet_hours_hold_a_request_arrival_until_morning(env: Env) -> None:
    work = env.work("Late Arrival")
    env.at(23, day=5)  # in the default quiet hours (21:00 to 07:00)
    env.request_arrived(MEMBER_ID, work, visibility="household")
    env.run()
    assert env.sink.to(MEMBER_ID) == []
    env.at(7, day=6)
    env.run()
    assert len(env.sink.to(MEMBER_ID)) == 1


# -- outages -----------------------------------------------------------------------------------


@pytest.mark.req("EF-INT-021")
def test_outage_over_one_hour_is_immediate_for_the_admin_even_at_night(env: Env) -> None:
    env.at(2, day=6)  # quiet hours: an outage is the exception
    env.outage()
    env.run()
    assert env.sink.sent == []  # not an hour yet
    env.advance(minutes=30)
    env.outage()  # still down (an outage is a run of the same event)
    env.run()
    assert env.sink.sent == []
    env.advance(minutes=31)
    env.outage()
    env.run()
    assert [u for u, _ in env.sink.sent] == [ADMIN_ID]
    env.advance(minutes=30)
    env.outage()
    env.run()
    assert len(env.sink.sent) == 1  # told once per outage, not at every failure


@pytest.mark.req("EF-INT-021")
def test_outage_is_told_once_even_when_no_new_event_comes(env: Env) -> None:
    """The sweep, not a new event, notices that an hour has gone by."""
    env.outage()
    env.advance(minutes=45)
    env.outage()
    env.advance(minutes=20)  # 65 minutes after the first, 20 after the last: still the same outage
    env.run()
    assert [u for u, _ in env.sink.sent] == [ADMIN_ID]


@pytest.mark.req("EF-INT-021")
def test_a_new_outage_after_a_recovery_is_told_again(env: Env) -> None:
    env.outage()
    env.advance(minutes=70)
    env.outage()
    env.run()
    env.advance(hours=5)  # nothing failed for five hours: it recovered
    env.outage()
    env.advance(minutes=65)
    env.outage()
    env.run()
    assert len(env.sink.to(ADMIN_ID)) == 2


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_outage_never_reaches_a_member_or_a_child(env: Env) -> None:
    for _ in range(3):
        env.outage()
        env.advance(minutes=40)
    env.run()
    assert [u for u, _ in env.sink.sent] == [ADMIN_ID]
    for other in (MEMBER_ID, OTHER_ID, CHILD_ID, SIBLING_ID):
        env.profile(other, notify_mode="immediate")
    for _ in range(3):
        env.outage("source.broken")
        env.advance(minutes=40)
    env.run()
    assert {u for u, _ in env.sink.sent} == {ADMIN_ID}


# -- the reminder of a withdrawal waiting for an admin -----------------------------------------


@pytest.mark.req("EF-INT-021", "EF-BIB-037")
def test_withdrawal_pending_reminds_daily_never_applies(env: Env) -> None:
    env.withdrawals[0] = 2  # two files wait to leave a family shelf
    env.at(9, day=5)
    env.run()
    assert env.sink.sent == []  # the reminder is part of tomorrow's digest
    for day in (6, 7, 8):
        env.at(8, day=day)
        env.run()
        env.at(9, day=day)
        env.run()  # a second sweep the same day changes nothing
    admin = env.sink.to(ADMIN_ID)
    assert len(admin) == 3  # once a day
    assert all("withdrawal" in m.body.lower() for m in admin)
    assert all(u == ADMIN_ID for u, _ in env.sink.sent)  # the admin only: no member, no child
    env.withdrawals[0] = 0  # the admin took them out
    env.at(8, day=9)
    env.run()
    env.at(8, day=10)
    env.run()
    assert len(env.sink.to(ADMIN_ID)) == 3  # no more reminders: and no empty message either


@pytest.mark.req("EF-INT-021", "EF-BIB-037")
def test_the_reminder_only_reads_it_has_no_door_to_apply_a_withdrawal(env: Env) -> None:
    import shelfhold.integrations.notify as notify
    import shelfhold.integrations.notify.service as service

    names = {*dir(notify), *dir(service)}
    assert not {n for n in names if "withdraw" in n.lower() and n != "pending_withdrawals"}
    env.withdrawals[0] = 1
    env.at(9, day=5)
    env.run()
    env.at(8, day=6)
    env.run()
    assert len(env.sink.to(ADMIN_ID)) == 1 and env.withdrawals == [1]  # read, never changed


# -- the attacks: who may be told about what ---------------------------------------------------


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_child_is_told_nothing_of_an_unrated_work_she_follows(env: Env) -> None:
    hidden = env.work("Hidden Unrated", age=None)
    visible = env.work("Fine For Kids", age=6)
    env.follow(CHILD_ID, hidden)  # a follow row she should not have, or one made before a rerate
    env.follow(CHILD_ID, visible)
    env.chapter(hidden)
    env.chapter(visible)
    env.profile(CHILD_ID, notify_mode="immediate")
    env.run()
    text = env.sink.text(CHILD_ID)
    assert "Fine For Kids" in text
    assert "Hidden Unrated" not in text and hidden not in text


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_child_is_told_nothing_of_a_work_above_her_ceiling(env: Env) -> None:
    above = env.work("Too Old For Her", age=16)  # her ceiling is 6
    env.follow(CHILD_ID, above)
    env.follow(SIBLING_ID, above)  # a ceiling of 10: also above
    env.follow(MEMBER_ID, above)
    for kid in (CHILD_ID, SIBLING_ID):
        env.profile(kid, notify_mode="immediate")
    env.chapter(above)
    env.request_arrived(CHILD_ID, above, visibility="household")
    env.at(8, day=6)
    env.run()
    assert env.sink.to(CHILD_ID) == [] and env.sink.to(SIBLING_ID) == []
    assert "Too Old For Her" in env.sink.text(MEMBER_ID)  # the adult is not narrowed


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_series_grouping_does_not_carry_a_hidden_title_to_a_child(env: Env) -> None:
    """A series whose member is hidden from her is hidden as a whole: its name, its count, and
    the visible chapter that would have lent it a cover."""
    shown = env.work("Shown Chapter", age=6)
    hidden = env.work("Spoiler Chapter", age=None)
    env.series("Series Holding A Hidden Member", [shown, hidden])
    env.follow(CHILD_ID, shown)
    env.profile(CHILD_ID, notify_mode="immediate")
    env.chapter(shown)
    env.at(8, day=6)
    env.run()
    assert env.sink.to(CHILD_ID) == []


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_a_work_rerated_after_the_event_is_not_sent_to_the_child(env: Env) -> None:
    """The decision is taken again when the message is built, not only when the row is queued."""
    work = env.work("Rerated Later", age=6)
    env.follow(CHILD_ID, work)
    env.chapter(work)
    assert env.pending() != []  # it was hers when it came
    env.rate(work, 16)  # the admin rated it up
    env.at(8, day=6)
    env.run()
    assert env.sink.to(CHILD_ID) == []
    assert env.pending() == []  # dropped, not kept for later


@pytest.mark.req("EF-INT-021", "EF-USR-006")
def test_a_ceiling_lowered_after_the_event_stops_the_message(env: Env) -> None:
    work = env.work("Ten Plus", age=10)
    env.follow(SIBLING_ID, work)  # a ceiling of 10
    env.chapter(work)
    env.profile(SIBLING_ID, max_age_rating=6)
    env.at(8, day=6)
    env.run()
    assert env.sink.to(SIBLING_ID) == []


@pytest.mark.req("EF-INT-021", "EF-USR-016")
def test_a_request_arrival_goes_to_the_requester_alone(env: Env) -> None:
    work = env.work("Private Wish")
    for follower in (OTHER_ID, ADMIN_ID, MEMBER_ID):
        env.follow(follower, work)
    env.request_arrived(MEMBER_ID, work, visibility="private")
    env.run()
    assert [u for u, _ in env.sink.sent] == [MEMBER_ID]
    assert "Private Wish" in env.sink.text(MEMBER_ID)


@pytest.mark.req("EF-INT-021", "EF-USR-016")
def test_a_private_title_is_never_in_another_persons_digest(env: Env) -> None:
    """A chapter that a member's private request brought is theirs: the other follower hears
    nothing, and the admin, who may oversee, is told without the title (it is masked)."""
    work = env.work("Quiet Reading")
    for follower in (OTHER_ID, ADMIN_ID, MEMBER_ID):
        env.follow(follower, work)
    env.chapter(work, actor=MEMBER_ID, visibility="private")
    env.at(8, day=6)
    env.run()
    assert env.sink.to(OTHER_ID) == []
    assert "Quiet Reading" in env.sink.text(MEMBER_ID)
    assert "Quiet Reading" not in env.sink.text(ADMIN_ID)  # masked for the admin


@pytest.mark.req("EF-INT-021", "EF-USR-016")
def test_a_childs_request_arrival_is_seen_by_the_parents_not_by_the_sibling(env: Env) -> None:
    work = env.work("Kids Pick", age=6)
    env.follow(SIBLING_ID, work)
    env.profile(SIBLING_ID, notify_mode="immediate")
    env.request_arrived(CHILD_ID, work, visibility="household")
    env.run()
    assert [u for u, _ in env.sink.sent] == [CHILD_ID]  # the requester, and no one else
    assert "Kids Pick" in env.sink.text(CHILD_ID)


@pytest.mark.req("EF-INT-021")
def test_a_user_who_is_gone_is_told_nothing(env: Env) -> None:
    work = env.work("Gone")
    env.follow(MEMBER_ID, work)
    env.chapter(work)
    with env.engine.begin() as connection:
        connection.exec_driver_sql('DELETE FROM "user" WHERE id = ?', (MEMBER_ID,))
    env.at(8, day=6)
    env.run()
    assert env.sink.sent == []
    assert env.pending() == []


# -- what is stored and what is logged ---------------------------------------------------------


@pytest.mark.req("EF-INT-021", "ENF-SEC-015")
def test_the_queue_holds_identifiers_and_counts_never_a_title(env: Env) -> None:
    work = env.work("A Very Distinctive Title")
    series = env.series("A Very Distinctive Series", [work])
    env.follow(MEMBER_ID, work)
    env.chapter(work)
    rows = env.pending()
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
    with world(settings, sink=sink) as env, capture_logs() as logs:
        env.at(9)
        work = env.work("Secret Plot Title")
        env.follow(MEMBER_ID, work)
        env.profile(MEMBER_ID, notify_mode="immediate")
        env.chapter(work)
        env.run()
        assert len(env.pending()) == 1  # kept for another try
        for _ in range(8):
            env.advance(hours=1)
            env.run()
        assert env.pending() == []  # gave up after a few tries
        sink.ok = True
        env.run()
        assert sink.sent == []  # and does not come back
    assert any(entry["event"].startswith("notify.") for entry in logs)  # it did say something
    assert "Secret Plot Title" not in str(logs)
