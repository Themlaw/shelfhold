"""EF-SRC-025 (a): the refresh Shelfhold asks of Suwayomi, its pace and its single document."""

import asyncio
import tomllib
from pathlib import Path

import pytest

from shelfhold.acquisition.application.follow import REFRESH_SPACING, refresh_delays
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from tests.fakes.suwayomi import BASE_URL, FakeSuwayomi, FakeSuwayomiFetcher
from tests.follow_env import new_chapter

ALLOWLIST = Path(__file__).resolve().parents[3] / "tools" / "production-reachability-allowlist.toml"


@pytest.mark.req("EF-SRC-025", "EF-SRC-009")
def test_refresh_is_spaced_per_upstream_source_with_a_jitter() -> None:
    delays = refresh_delays(["a", "a", "b", "a", "b"], draw=lambda low, high: high)
    assert delays == [0.0, REFRESH_SPACING + 1.0, 0.0, REFRESH_SPACING + 1.0, REFRESH_SPACING + 1.0]
    first = refresh_delays(["a", "b", "c"])
    assert first == [0.0, 0.0, 0.0]  # a source is never made to wait before its first refresh
    for delay in refresh_delays(["a"] * 5)[1:]:
        assert REFRESH_SPACING <= delay <= REFRESH_SPACING + 1.0


@pytest.mark.req("EF-SRC-025")
def test_driven_service_sends_only_fetch_chapters_and_learns_the_news() -> None:
    server = FakeSuwayomi(auth_mode="none")
    service = SuwayomiService(BASE_URL, refresh_mode="driven")
    f = FakeSuwayomiFetcher(server)
    page = asyncio.run(service.search(f, "", {}, None))
    alpha = next(ref for ref in page.items if ref.title == "Series Alpha")
    server.publish_upstream(new_chapter(5030, 3), 101)
    before = len(asyncio.run(service.list_units(f, alpha)))
    asyncio.run(service.request_refresh(f, [alpha]))
    assert len(asyncio.run(service.list_units(f, alpha))) == before + 1
    sent = set().union(*(r.fields for r in server.requests))
    assert "fetchChapters" in sent and not {"updateLibrary", "fetchManga"} & sent
    assert server.violations == [] and server.refreshed == [101]


@pytest.mark.req("EF-SRC-025", "EF-INT-022")
def test_the_allowlist_no_longer_carries_request_refresh() -> None:
    """`request_refresh` has its production caller (the piloted follow cycle): its entry is gone."""
    text = ALLOWLIST.read_text("utf-8")
    assert "request_refresh" not in text and "unwired-m1b-l21" not in text
    assert tomllib.loads(text)["allow"]
