"""What L17 adds to the Suwayomi adapter and to the host, read from the source (EF-SRC-026).

`updateChapters` and `UpdateChapterPatch { isBookmarked, isRead, lastPageRead }` are those of
`ChapterMutation.kt` at the tag v2.4.2366, the minimum version Shelfhold supports: the cassette-like
simulated server (`tests/fakes/suwayomi`) is synthetic and follows that source.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from shelfhold.headless.errors import InvalidConfigValue
from shelfhold.headless.keys import KEYS
from shelfhold.jobs.kinds import traits_of
from shelfhold.jobs.scheduler import priority_class_of
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.plugin_host.adapters.meta.suwayomi import queries as q
from shelfhold.plugin_host.adapters.meta.suwayomi.read_state import SuwayomiReadState
from shelfhold.runtime_jobs import READ_STATE_PUSH_KIND
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher

pytestmark = pytest.mark.req("EF-SRC-026")

SRC = Path(__file__).resolve().parents[2] / "src" / "shelfhold"


def test_the_mutation_is_the_one_read_in_the_source_with_literal_ints_and_isread_alone() -> None:
    assert q.update_chapters_read([5001, 5002], True) == (
        "mutation { updateChapters(input: {ids: [5001, 5002], patch: {isRead: true}}) "
        "{ chapters { id isRead } } }"
    )
    assert "isRead: false" in q.update_chapters_read([1], False)
    for field in ("lastPageRead", "isBookmarked"):
        assert field not in q.update_chapters_read([1], True)


def test_the_ids_are_whole_numbers_whatever_they_are_given() -> None:
    with pytest.raises(ValueError, match="chapter id"):
        q.update_chapters_read([1, "2}) { x } #"], True)  # type: ignore[list-item]
    with pytest.raises(ValueError, match="chapter id"):
        q.update_chapters_read([], True)
    with pytest.raises(ValueError, match="chapter id"):
        q.update_chapters_read([True], True)


def test_the_chapter_listing_asks_for_the_read_flag_and_nothing_more() -> None:
    assert "isRead" in q.READ_CHAPTERS and "url" in q.READ_CHAPTERS
    for field in ("lastPageRead", "isBookmarked", "pageCount"):
        assert field not in q.READ_CHAPTERS


def test_the_adapter_lists_chapters_by_unit_key_and_writes_through_the_fake() -> None:
    server = FakeSuwayomi(auth_mode="none")
    remote = SuwayomiReadState(SuwayomiService(BASE_URL), FakeSuwayomiFetcher(server))

    async def go() -> None:
        listed = await remote.chapters("suwayomi:7001", "/series-alpha")
        assert listed["suwayomi:7001:/series-alpha/ch-001"].chapter_id == 5001
        assert not listed["suwayomi:7001:/series-alpha/ch-001"].is_read
        await remote.set_read([5001], read=True)
        again = await remote.chapters("suwayomi:7001", "/series-alpha")
        assert again["suwayomi:7001:/series-alpha/ch-001"].is_read

    asyncio.run(go())
    assert server.violations == []


def test_the_job_is_a_background_kind_in_the_lowest_class() -> None:
    assert READ_STATE_PUSH_KIND == "read_state.push"
    assert traits_of(READ_STATE_PUSH_KIND).background
    assert priority_class_of(READ_STATE_PUSH_KIND, windowed=False) == "backfill"


def test_the_owner_key_is_a_user_name_empty_for_the_administrator() -> None:
    key = KEYS["source.read_state_owner"]
    assert key.default == ""
    assert key.validate is not None
    assert key.validate("  Reader ") == "Reader"
    assert key.validate("") == ""
    with pytest.raises(InvalidConfigValue):
        key.validate("x" * 200)


def test_no_http_route_nor_command_can_start_a_push() -> None:
    """The job is the only door: no key, no device token, no profile reaches the push."""
    for folder in ("interfaces", "integrations"):
        for path in (SRC / folder).rglob("*.py"):
            text = path.read_text("utf-8")
            assert "read_state_push" not in text, path
            assert "SuwayomiReadState" not in text, path
