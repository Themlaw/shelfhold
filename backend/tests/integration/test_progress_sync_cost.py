"""What a pass of the progress synchronisation costs (M4 L14b, review of #292): a pass that finds
nothing new asks Kavita for next to nothing and writes next to nothing; the pages of an EPUB are
left alone; a note belongs to the file and the account it was made for."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shelfhold.infrastructure.db import write_transaction
from tests.sync_env import sync_env


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_a_pass_over_unchanged_chapters_makes_a_few_calls(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapters = [env.chapter(f"Series {s}", n) for s in range(6) for n in range(1, 21)]
        env.device_position(reader, chapters[3], 9)
        env.clock.advance(minutes=1)
        env.remote._structure_ttl = 1800.0  # as in production: the structure is kept between passes
        env.sync(reader)  # the first pass asks about everything and pushes the one position
        env.clock.advance(minutes=10)
        env.sync(reader)  # Kavita's stamps now show the push: one more look, then it is quiet
        env.clock.advance(minutes=10)
        before = len(env.fake.requests)
        assert env.sync(reader).code == "ok"
        calls = len(env.fake.requests) - before
        assert len(chapters) == 120
        assert calls <= 3, (
            f"{calls} calls for a pass with nothing new"
        )  # whoami and the series list


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_only_the_series_that_moved_in_kavita_are_asked_about(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        quiet = [env.chapter("Quiet", n) for n in range(1, 8)]
        busy = [env.chapter("Busy", n) for n in range(1, 4)]
        env.sync(reader)
        env.clock.advance(minutes=10)
        env.sync(reader)
        env.read_in_kavita(reader, busy[1], 12)  # read in Kavita's own reader
        env.clock.advance(minutes=10)
        before = len(env.calls(env.keys[reader]))
        outcome = env.sync(reader)
        asked = [r for r in env.calls(env.keys[reader])[before:] if "get-progress" in r.path]
        assert outcome.pulled == 1
        assert len(asked) == len(busy)  # the series that moved, none of the other
        assert quiet and env.state(reader, busy[1]) == "in_progress"


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_a_new_position_is_pushed_without_asking_about_the_idle_series(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        quiet = [env.chapter("Quiet", n) for n in range(1, 8)]
        busy = env.chapter("Busy", 1)
        env.sync(reader)
        env.clock.advance(minutes=10)
        env.sync(reader)
        env.device_position(reader, busy, 6)
        env.clock.advance(minutes=1)
        before = len(env.calls(env.keys[reader]))
        env.sync(reader)
        asked = [r for r in env.calls(env.keys[reader])[before:] if "reader/" in r.path.lower()]
        assert env.kavita_page(reader, busy) == 6
        assert len(asked) <= 3  # two reads of the one chapter and the write
        assert quiet


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_one_connection_serves_the_whole_pass_of_a_member(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        for n in range(1, 6):
            env.chapter("Orchard Road", n)
        opened: list[Any] = []
        make = env.remote._fetcher_for

        def counting(url: str) -> Any:
            opened.append(url)
            return make(url)

        env.remote._fetcher_for = counting
        env.sync(reader)
        assert len(opened) == 1


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_an_epub_is_neither_pulled_nor_pushed(tmp_path: Path) -> None:
    """Kavita counts an EPUB in book documents, not in the pages the Units are bounded in."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        book = env.chapter("Novel", 1, ext="epub")
        paged = env.chapter("Orchard Road", 1)
        env.device_position(reader, book, 8)
        env.read_in_kavita(reader, book, 5)
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, book) == 5  # nothing pushed
        assert env.positions(reader, source="server") == []  # nothing pulled
        assert paged.pages == 20


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_a_chapter_id_now_held_by_another_file_inherits_no_note(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        first = env.chapter("Orchard Road", 1)
        env.device_position(reader, first, 15)
        env.clock.advance(minutes=1)
        env.sync(reader)
        other = env.chapter("Elsewhere", 1)
        with write_transaction(env.engine) as connection:  # Kavita reuses the id for another file
            connection.exec_driver_sql(
                "UPDATE progress_sync_note SET carrier_id = ?", (other.carrier,)
            )
        env.clock.advance(minutes=10)
        env.sync(reader)
        with env.engine.connect() as connection:
            stored = connection.exec_driver_sql("SELECT carrier_id FROM progress_sync_note").all()
        assert (first.carrier,) in [tuple(r) for r in stored]  # the file the chapter holds now


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_a_stop_between_two_series_leaves_the_rest_for_the_next_pass(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        env.chapter("Alpha", 1)
        env.chapter("Beta", 1)
        asked = SimpleNamespace(count=0)

        def stop() -> bool:
            asked.count += 1
            return bool(asked.count > 1)  # allowed for the first series only

        from shelfhold.reading.application.sync import sync_member

        sync_member(
            env.engine, env.remote, user_id=reader, destination_id=env.destination, clock=env.clock,
            should_stop=stop,
        )  # fmt: skip
        assert len([r for r in env.calls(env.keys[reader]) if "get-progress" in r.path]) == 1
