"""The read state pushed back to Suwayomi (M4 L17, EF-SRC-026, D12).

Suwayomi has one user. The state pushed is the one of the **owner profile** (the setting
`source.read_state_owner`, the administrator by default), taken from the `reading_progress` view
and from nowhere else: a traversal or a state a server holds, never a look at the last page, never
a child, never another member. The simulated server follows `ChapterMutation.kt` (tag v2.4.2366).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfhold.infrastructure.db import write_transaction
from shelfhold.reading.public import undo_read_mark
from tests.read_state_env import ReadStateEnv, read_state_env

pytestmark = pytest.mark.req("EF-SRC-026")


def undo(env: ReadStateEnv, user: str, chapter_unit: str) -> None:
    with write_transaction(env.engine) as connection:
        assert (
            undo_read_mark(
                connection, viewer_id=user, subject_id=user, unit_id=chapter_unit, now=env.clock.now()
            )
            == 1
        )


def test_read_state_pushed_to_suwayomi(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        admin = env.reading.users["root"]
        first, second, third = env.chapter(), env.chapter(), env.chapter()
        env.traversal(admin, first)
        env.server_read(admin, second)
        report = env.push()
        assert report.code == "ok"
        assert (report.pushed_read, report.pushed_unread) == (2, 0)
        assert env.is_read_there(first) and env.is_read_there(second)
        assert not env.is_read_there(third)
        assert env.fake.read_updates == [([first.suwayomi_id, second.suwayomi_id], True)]  # a batch
        assert env.fake.violations == []


def test_child_read_state_never_pushed(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        child = env.user("kid", role="child")
        chapter = env.chapter()
        env.traversal(child, chapter)
        assert env.state(child, chapter) == "read"
        report = env.push()
        assert not env.is_read_there(chapter)
        assert env.fake.read_updates == []
        assert report.pushed_read == 0
        assert env.notes() == []


def test_a_child_named_as_owner_pushes_nothing_and_no_one_takes_her_place(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        child = env.user("kid", role="child")
        admin = env.reading.users["root"]
        chapter = env.chapter()
        env.traversal(child, chapter)
        env.traversal(admin, chapter)
        env.set_owner("kid")
        report = env.push()
        assert report.code == "owner_is_child"
        assert env.calls() == 0  # not even a read of the server
        assert env.fake.read_updates == []


def test_an_unknown_owner_stops_the_push_without_a_fallback(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        chapter = env.chapter()
        env.traversal(env.reading.users["root"], chapter)
        env.set_owner("nobody-here")
        assert env.push().code == "owner_unknown"
        assert env.calls() == 0


def test_a_member_is_never_pushed_while_the_owner_is_the_administrator(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        member = env.user("reader")
        mine, theirs = env.chapter(), env.chapter()
        env.traversal(member, mine)
        env.traversal(env.reading.users["root"], theirs)
        env.push()
        assert not env.is_read_there(mine)
        assert env.is_read_there(theirs)


def test_a_designated_member_is_the_owner_and_the_administrator_is_not(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        member = env.user("reader")
        mine, theirs = env.chapter(), env.chapter()
        env.traversal(member, mine)
        env.traversal(env.reading.users["root"], theirs)
        env.set_owner("Reader")  # a user name, the case of the column
        env.push()
        assert env.is_read_there(mine) and not env.is_read_there(theirs)


def test_a_look_at_the_last_page_is_never_pushed(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        admin = env.reading.users["root"]
        looked = env.chapter()
        env.peek(admin, looked)
        assert env.state(admin, looked) == "in_progress"
        env.push()
        assert env.fake.read_updates == []
        assert not env.is_read_there(looked)


def test_an_undone_mark_is_propagated_and_nothing_else_is_touched(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        admin = env.reading.users["root"]
        kept, undone = env.chapter(), env.chapter()
        env.traversal(admin, kept)
        env.traversal(admin, undone)
        env.push()
        assert env.is_read_there(undone)
        env.clock.advance(minutes=5)
        undo(env, admin, undone.unit)
        report = env.push()
        assert (report.pushed_read, report.pushed_unread) == (0, 1)
        assert env.fake.read_updates[-1] == ([undone.suwayomi_id], False)
        assert not env.is_read_there(undone) and env.is_read_there(kept)
        assert env.push().pushed_unread == 0  # said once


def test_a_chapter_read_in_suwayomi_by_someone_else_is_never_marked_unread(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        chapter = env.chapter()
        env.read_there(chapter)  # Suwayomi's own user read it; Shelfhold never pushed anything
        env.push()
        assert env.fake.read_updates == []
        assert env.is_read_there(chapter)


def test_push_is_idempotent_and_sends_only_what_changed(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        admin = env.reading.users["root"]
        first, second = env.chapter(), env.chapter()
        env.traversal(admin, first)
        env.push()
        before = env.calls()
        again = env.push()
        assert env.calls() == before  # nothing changed: not one request
        assert (again.pushed_read, again.pushed_unread, again.series) == (0, 0, 0)
        env.clock.advance(minutes=1)
        env.traversal(admin, second)
        env.push()
        assert env.fake.read_updates == [
            ([first.suwayomi_id], True),
            ([second.suwayomi_id], True),
        ]


def test_a_chapter_already_read_in_suwayomi_is_noted_not_written(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        chapter = env.chapter()
        env.read_there(chapter)
        env.traversal(env.reading.users["root"], chapter)
        report = env.push()
        assert env.fake.read_updates == []
        assert report.already == 1
        assert [n.state for n in env.notes()] == ["read"]
        before = env.calls()
        env.push()
        assert env.calls() == before


def test_only_chapters_held_in_the_store_are_pushed(tmp_path: Path) -> None:
    """m10: a push must not change what Suwayomi downloads or keeps for a chapter Shelfhold
    does not hold."""
    with read_state_env(tmp_path) as env:
        admin = env.reading.users["root"]
        held, not_held = env.chapter(), env.chapter(copied=False)
        env.traversal(admin, held)
        env.traversal(admin, not_held)
        env.push()
        assert env.is_read_there(held) and not env.is_read_there(not_held)


def test_the_chapters_go_in_batches_one_request_per_batch(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        admin = env.reading.users["root"]
        many = [env.chapter() for _ in range(5)]
        for chapter in many:
            env.traversal(admin, chapter)
        env.push(batch=2)
        assert [len(ids) for ids, _ in env.fake.read_updates] == [2, 2, 1]
        assert all(env.is_read_there(c) for c in many)
        listings = [r for r in env.fake.requests if "chapters(condition" in r.json()["query"]]
        assert len(listings) == 1  # the series is listed once, however many batches follow


def test_a_sleeping_service_is_a_pause_and_is_not_woken(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        chapter = env.chapter()
        env.traversal(env.reading.users["root"], chapter)
        report = env.push(awake=lambda: False)
        assert report.code == "asleep"
        assert env.calls() == 0
        assert env.push().pushed_read == 1  # nothing was lost: the next pass pushes


def test_an_unreachable_service_is_a_pause_nothing_is_lost(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        chapter = env.chapter()
        env.traversal(env.reading.users["root"], chapter)
        env.fake.asleep = True
        assert env.push().code == "unavailable"
        assert env.notes() == []
        env.fake.wake_up()
        assert env.push().pushed_read == 1
        assert env.is_read_there(chapter)


def test_piloted_mode_no_refresh_no_echo(tmp_path: Path) -> None:
    """The push neither asks Suwayomi to refresh (the check of the updates) nor reads its state
    back: `isRead` never becomes a position."""
    with read_state_env(tmp_path) as env:
        admin = env.reading.users["root"]
        chapter = env.chapter()
        env.traversal(admin, chapter)
        positions = env.positions()
        env.push()
        env.read_there(env.chapter())  # Suwayomi's own user reads another one
        env.push()
        assert "fetchChapters" not in env.graphql_fields()
        assert env.fake.refreshed == []
        assert env.positions() == positions
        assert env.fake.violations == []


def test_a_stop_is_read_between_two_series(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        env.traversal(env.reading.users["root"], env.chapter())
        assert env.push(should_stop=lambda: True).code == "stopped"
        assert env.calls() == 0
