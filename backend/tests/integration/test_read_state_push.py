"""The read state pushed back to Suwayomi (M4 L17, EF-SRC-026, D12).

Suwayomi has one user. The state pushed is the one of the **owner profile** (the setting
`source.read_state_owner`, the administrator by default), taken from the `reading_progress` view
and from nowhere else: a traversal or a state a server holds, never a look at the last page, never
a child, never another member. The simulated server follows `ChapterMutation.kt` (tag v2.4.2366).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.infrastructure.db import write_transaction
from shelfhold.reading.public import undo_read_mark
from tests.read_state_env import ReadStateEnv, read_state_env

pytestmark = pytest.mark.req("EF-SRC-026")


def undo(env: ReadStateEnv, user: str, chapter_unit: str) -> None:
    with write_transaction(env.engine) as connection:
        assert (
            undo_read_mark(
                connection,
                viewer_id=user,
                subject_id=user,
                unit_id=chapter_unit,
                now=env.clock.now(),
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


def context() -> Any:
    counters: dict[str, object] = {}

    def add(**counts: int) -> None:
        for name, amount in counts.items():
            counters[name] = int(str(counters.get(name, 0))) + amount

    return SimpleNamespace(counters=counters, record=counters.update, add=add, stop_requested=False)


def test_the_job_pushes_and_counts(tmp_path: Path) -> None:
    from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiReadState, SuwayomiService
    from shelfhold.runtime_jobs import read_state_push_handler
    from tests.fakes.suwayomi import BASE_URL

    with read_state_env(tmp_path) as env:
        chapter = env.chapter()
        env.traversal(env.reading.users["root"], chapter)
        remote = SuwayomiReadState(SuwayomiService(BASE_URL), env.fetcher)
        ctx = context()
        read_state_push_handler(env.engine, lambda: remote, env.clock)(ctx)
        assert ctx.counters["pushed_read"] == 1 and env.is_read_there(chapter)
        asleep = context()
        env.traversal(env.reading.users["root"], env.chapter())
        read_state_push_handler(env.engine, lambda: remote, env.clock, lambda: False)(asleep)
        assert asleep.counters["paused"] == 1 and "stopped" not in asleep.counters
        child = env.user("kid", role="child")
        env.set_owner("kid")
        refused = context()
        read_state_push_handler(env.engine, lambda: remote, env.clock)(refused)
        assert refused.counters["stopped"] == 1
        assert child


def test_the_job_is_idle_without_suwayomi_or_with_a_locked_keyring(tmp_path: Path) -> None:
    from shelfhold.composition import KeyringUnavailableError
    from shelfhold.runtime_jobs import read_state_push_handler

    with read_state_env(tmp_path) as env:
        none = context()
        read_state_push_handler(env.engine, lambda: None, env.clock)(none)
        assert none.counters["skipped"] == "no_suwayomi"

        def locked() -> Any:
            raise KeyringUnavailableError

        sealed = context()
        read_state_push_handler(env.engine, locked, env.clock)(sealed)
        assert sealed.counters["skipped"] == "keyring_unavailable"


def test_a_series_gone_from_suwayomi_is_skipped_and_the_others_go_on(tmp_path: Path) -> None:
    with read_state_env(tmp_path) as env:
        chapter = env.chapter()
        env.traversal(env.reading.users["root"], chapter)
        manga = next(m for m in env.fake.state["mangas"] if m["url"] == "/series-alpha")
        manga["url"] = "/moved"  # the series is no longer where the binding says
        report = env.push()
        assert (report.code, report.missing) == ("ok", 1)
        assert env.notes() == []


def test_a_refusal_of_suwayomi_ends_the_pass_with_its_class(tmp_path: Path) -> None:
    import asyncio

    from shelfhold.acquisition.application.read_state_push import RemoteRefused, push_read_state

    class Refusing:
        async def chapters(self, source_key: str, remote_id: str) -> dict[str, Any]:
            raise RemoteRefused("auth")

        async def set_read(self, chapter_ids: Any, *, read: bool) -> None:
            raise AssertionError("nothing is written after a refusal")

    with read_state_env(tmp_path) as env:
        env.traversal(env.reading.users["root"], env.chapter())
        report = asyncio.run(push_read_state(env.engine, Refusing(), clock=env.clock))
        assert report.code == "refused" and env.notes() == []


def test_a_wake_session_decides_whether_the_service_is_awake() -> None:
    from datetime import timedelta
    from unittest.mock import Mock

    import sqlalchemy as sa

    from shelfhold.infrastructure.wake import Wakes
    from tests.fakes_clock import ManualClock
    from tests.read_state_env import START

    clock = ManualClock(START)
    engine = sa.create_engine("sqlite://")
    wakes = Wakes(
        Mock(), frozenset({"suwayomi"}), engine=engine, bus=Mock(), clock=clock,
        session_duration=timedelta(minutes=10),
    )  # fmt: skip
    assert wakes.is_awake("anything-else")  # not behind the hook: always up
    assert not wakes.is_awake("suwayomi")  # attached, no session open
    wakes._awake_until["suwayomi"] = clock.now() + timedelta(minutes=5)
    assert wakes.is_awake("suwayomi")
    clock.advance(minutes=6)
    assert not wakes.is_awake("suwayomi")


def test_no_administrator_means_no_owner(tmp_path: Path) -> None:
    import sqlalchemy as sa

    from shelfhold.acquisition.application.read_state_push import resolve_owner

    with read_state_env(tmp_path) as env:
        with write_transaction(env.engine) as connection:
            connection.execute(sa.text("DELETE FROM user WHERE role = 'admin'"))
        with env.engine.connect() as connection:
            assert resolve_owner(connection) == (None, "owner_unknown")
