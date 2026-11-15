"""Progress synchronisation with Kavita (M4 L14b, EF-EXP-026, EF-LEC-014, D11).

Pull first, push last, last write wins on the **server's** time, each member with their own key.
Kavita is simulated (`tests.fakes.kavita`): its progress routes follow `ReaderController` and
`ReaderService.SaveReadingProgress` of the tag v0.9.1.4 (see `docs/reference/progress-sync.md`):
a page per chapter and per account, 0 to the page count, the count meaning "read".
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from tests.fakes.kavita import API_KEY
from tests.sync_env import START, sync_env


def progress_calls(env, key: str | None = None):  # type: ignore[no-untyped-def]
    return env.calls(key, fragment="/api/reader/")


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_pull_progress_from_kavita_first(tmp_path: Path) -> None:
    """Kavita holds page 7, a reader of Shelfhold is at page 12: the page of Kavita is read before
    anything is written, and only then is the newer position pushed."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, 7)
        env.clock.advance(minutes=5)
        env.device_position(reader, chapter, 12)
        env.clock.advance(minutes=1)
        outcome = env.sync(reader)
        assert outcome.code == "ok"
        pulled = env.positions(reader, source="server")
        assert [p.locator["page"] for p in pulled] == [7]  # what Kavita said, taken as it is
        assert pulled[0].device_id == f"server:{env.destination}"
        verbs = [(c.method, c.path.lower()) for c in progress_calls(env)]
        assert verbs[0] == ("GET", "/api/reader/get-progress")  # the pull came first
        assert verbs[-1] == ("POST", "/api/reader/progress")  # the push came last
        assert env.kavita_page(reader, chapter) == 12
        assert env.fake.violations == []


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_push_progress_lww_server_timestamp(tmp_path: Path) -> None:
    """Two devices: the later one by the server's clock wins, whatever its `client_ts` says."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        far_future = START + timedelta(days=900)
        env.device_position(reader, chapter, 12, device="reader-1", client_ts=far_future)
        env.clock.advance(minutes=3)
        env.device_position(reader, chapter, 8, device="reader-2", client_ts=START)
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == 8  # reader-2 wrote last, on the server's time
        assert [w[2] for w in env.fake.progress_writes] == [8]


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_sync_uses_each_member_token(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        one, two = env.member("one"), env.member("two")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(one, chapter, 5)
        env.device_position(two, chapter, 9)
        env.clock.advance(minutes=1)
        reports = env.sync_all()
        assert {u: r.code for u, r in reports.items()} == {one: "ok", two: "ok"}
        assert progress_calls(env, env.keys[one]) and progress_calls(env, env.keys[two])
        assert progress_calls(env, API_KEY) == []  # never the service account's key
        assert env.kavita_page(one, chapter) == 5
        assert env.kavita_page(two, chapter) == 9


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_pulled_position_written_only_to_token_owner(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        one, two, bystander = env.member("one"), env.member("two"), env.member("by", link=False)
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(one, chapter, 4)
        env.read_in_kavita(two, chapter, 11)
        env.sync_all()
        assert [p.locator["page"] for p in env.positions(one, source="server")] == [4]
        assert [p.locator["page"] for p in env.positions(two, source="server")] == [11]
        assert env.positions(bystander) == []  # no key, no state, nothing written for them


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_push_never_moves_kavita_position_backwards(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        behind, finished = env.chapter("Orchard Road", 1), env.chapter("Orchard Road", 2)
        env.read_in_kavita(reader, behind, 15)
        env.read_in_kavita(reader, finished, 20)  # Kavita says: read
        env.clock.advance(minutes=1)
        env.device_position(reader, behind, 9)  # newer, but behind
        env.device_position(reader, finished, 19)
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.fake.progress_writes == []  # nothing was written to Kavita
        assert env.kavita_page(reader, behind) == 15
        assert env.kavita_page(reader, finished) == 20


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_progress_written_as_opds_pse_client_is_pulled(tmp_path: Path) -> None:
    """The proof of D-P5: what an OPDS-PSE client leaves in Kavita (Kavita saves the page it served,
    `OPDSController`, with the series, volume and chapter of the file) is what Shelfhold pulls, and
    what Shelfhold writes is the same `ProgressDto` (page, volume, chapter, series, library)."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, 5)  # a page served to a reader app
        env.sync(reader)
        assert env.state(reader, chapter) == "in_progress"
        env.read_in_kavita(reader, chapter, chapter.pages)  # Kavita counts the chapter read
        env.sync(reader)
        assert env.state(reader, chapter) == "read"
        env.clock.advance(minutes=2)
        other = env.chapter("Orchard Road", 2)
        env.device_position(reader, other, 6)
        env.sync(reader)
        body = [c.json() for c in progress_calls(env) if c.method == "POST"]
        assert body == [
            {
                "volumeId": other.chapter_id // 100 * 100, "chapterId": other.chapter_id,
                "pageNum": 6, "seriesId": other.chapter_id // 100, "libraryId": 7,
            }
        ]  # fmt: skip
        assert env.fake.violations == []


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_kavita_unfinished_chapter_never_reads_as_a_look_at_the_end(tmp_path: Path) -> None:
    """Kavita at its last page index (not finished) is `in_progress` here, whichever way the pages
    of a file are counted: only the page count, Kavita's own "read", makes a server state read."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, chapter.pages - 1)
        env.sync(reader)
        assert env.state(reader, chapter) == "in_progress"


# ------------------------------------------------------------------ the contract of L14a


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_a_look_at_the_last_page_pushed_to_kavita_does_not_come_back_as_read(
    tmp_path: Path,
) -> None:
    """KOReader, a look at the last page; pushed to Kavita; pulled again: never washed into a
    reading. Kavita is given the page, not the mark, and its echo is not read back."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, chapter.pages)  # the very last page, a look
        env.clock.advance(minutes=1)
        for _ in range(3):  # and again: the round trip is stable
            env.sync(reader)
            env.clock.advance(minutes=10)
        assert env.kavita_page(reader, chapter) == chapter.pages - 1  # the page, not the mark
        assert env.state(reader, chapter) == "in_progress"
        assert env.positions(reader, source="server") == []  # the echo was dropped


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_a_chapter_a_traversal_marked_read_is_pushed_as_read(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.reading.position(  # the end of a traversal that marked the Unit (a journal)
            reader, chapter.carrier, page=chapter.pages - 1, percent=1.0, source="device",
            content_hash=env.content_hash(chapter), client_ts=START, server_ts=START,
            read_units=chapter.units,
        )  # fmt: skip
        assert env.state(reader, chapter) == "read"
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == chapter.pages
        env.clock.advance(minutes=10)
        env.sync(reader)
        assert env.positions(reader, source="server") == []  # the echo is dropped, not re-read


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_what_kavita_marks_read_by_itself_is_taken_as_it_is(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, chapter.pages)
        env.sync(reader)
        assert env.state(reader, chapter) == "read"
        assert len(env.positions(reader, source="server")) == 1


# ------------------------------------------------------------------ races and clocks


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_race_kavita_behind_shelfhold_ahead_is_pushed(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, 3)
        env.clock.advance(minutes=1)
        env.device_position(reader, chapter, 12)
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == 12


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_race_kavita_ahead_is_pulled_and_not_overwritten(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, 12)
        env.clock.advance(minutes=1)
        env.read_in_kavita(reader, chapter, 18)  # the other reader app went further
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == 18
        assert env.fake.progress_writes == []
        server = env.positions(reader, source="server")
        assert [p.locator["page"] for p in server] == [18]


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
@pytest.mark.parametrize("skew_days", [3, -3, 400])
def test_a_skewed_kavita_clock_decides_nothing(tmp_path: Path, skew_days: int) -> None:
    """Kavita's `lastModifiedUtc` is its own clock: it only says "this changed". The order is the
    one of Shelfhold's clock, so a Kavita days in the future (or the past) neither blocks a later
    push nor makes a pulled state older or newer than it is."""
    with sync_env(tmp_path) as env:
        env.fake.clock_skew = timedelta(days=skew_days)
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, 6)
        env.clock.advance(minutes=1)
        env.sync(reader)
        pulled = env.positions(reader, source="server")
        assert [p.locator["page"] for p in pulled] == [6]
        assert str(pulled[0].server_ts).startswith("2026-11-15 12:01")  # Shelfhold's own clock
        env.clock.advance(minutes=1)
        env.device_position(reader, chapter, 15)  # later by Shelfhold's clock
        env.clock.advance(minutes=1)
        env.sync(reader)
        assert env.kavita_page(reader, chapter) == 15


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_no_oscillation_between_shelfhold_and_kavita(tmp_path: Path) -> None:
    """Whatever the starting point, the two sides settle: a pass that finds nothing new writes
    nothing, and a push is never read back as news."""
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, 5)
        env.clock.advance(minutes=1)
        env.device_position(reader, chapter, 12)
        env.clock.advance(minutes=1)
        env.sync(reader)  # pull 5, push 12
        env.clock.advance(minutes=30)
        env.read_in_kavita(reader, chapter, 14)  # Kavita moves on
        env.clock.advance(minutes=1)
        env.sync(reader)  # pull 14: nothing to push
        settled = (len(env.positions(reader)), list(env.fake.progress_writes))
        for _ in range(6):
            env.clock.advance(minutes=30)
            env.sync(reader)
        assert (len(env.positions(reader)), list(env.fake.progress_writes)) == settled
        assert env.kavita_page(reader, chapter) == 14


# ------------------------------------------------------------------ resilience


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_a_429_is_waited_for_then_retried_within_a_cap(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, 4)
        env.fake.fail_progress(429, retry_after="2")
        outcome = env.sync(reader)
        assert outcome.code == "ok"
        assert env.sleeps == [2.0]
        assert [p.locator["page"] for p in env.positions(reader, source="server")] == [4]
        env.fake.fail_progress(429, retry_after="86400")  # a day: waited for, but capped
        env.clock.advance(minutes=30)
        env.read_in_kavita(reader, chapter, 5)
        env.sync(reader)
        assert max(env.sleeps) <= 30


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_kavita_down_pauses_the_synchronisation_and_never_fails(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, 9)
        env.clock.advance(minutes=1)
        env.fake.fail_progress(503, times=50)
        before = len(env.calls())
        outcome = env.sync(reader)  # raises nothing
        assert outcome.code == "unavailable"
        assert len(env.calls()) - before <= 5  # bounded attempts
        assert env.fake.progress_writes == []
        env.fake.progress_failures.clear()
        env.clock.advance(minutes=5)
        assert env.sync(reader).code == "ok"  # nothing was lost: the position is pushed now
        assert env.kavita_page(reader, chapter) == 9


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_a_dead_kavita_connection_is_a_pause(tmp_path: Path) -> None:
    with sync_env(tmp_path) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.device_position(reader, chapter, 9)
        env.fake.asleep = True
        assert env.sync(reader).code == "unavailable"


@pytest.mark.req("EF-EXP-026", "EF-LEC-014")
def test_a_destination_without_progress_sync_is_left_alone(tmp_path: Path) -> None:
    with sync_env(tmp_path, enabled=False) as env:
        reader = env.member("reader")
        chapter = env.chapter("Orchard Road", 1)
        env.read_in_kavita(reader, chapter, 4)
        assert env.sync(reader).code == "not_enabled"
        assert env.calls() == []
        assert env.sync_all() == {}
