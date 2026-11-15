"""The calls of the progress synchronisation against Kavita 0.9.1 (M4 L14b, EF-EXP-026, D8).

Contract read in the source of Kavita on GitHub, tag `v0.9.1.4` (`gh api repos/Kareadita/Kavita/
contents/<path>?ref=v0.9.1.4`), and written down in `docs/reference/progress-sync.md`:

* `GET /api/Reader/get-progress?chapterId=` answers a `ProgressDto` for the account of the key;
  with no progress it answers a stub (page 0, volume, series and library 0);
* `POST /api/Reader/progress` takes a `ProgressDto` (`volumeId`, `chapterId`, `pageNum`,
  `seriesId`, `libraryId` required) and answers 200 with no body; the page is capped to the
  chapter's page count (`CapPageToChapter`), an unchanged page changes nothing;
* a chapter is read when `pagesRead >= pages`.

The cassettes are **synthetic** (they say so): fabricated from that source, no recording, every id
and key made up. They are replayed through the production fetcher, with no network.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx2
import pytest

from shelfhold.export.targets.kavita.accounts import MemberKavitaClient, MemberKey
from shelfhold.export.targets.kavita.progress import MemberProgress
from shelfhold.infrastructure.fetcher import IntegrationFetcher, LanTargets
from shelfhold.reading.application.sync import ChapterRef, RemoteUnavailable
from tests.fakes_fetcher import FakeResolver

pytestmark = pytest.mark.req("EF-EXP-026", "EF-USR-012")

CASSETTES = Path(__file__).resolve().parents[1] / "cassettes" / "kavita_progress"
PINNED = Path(__file__).resolve().parents[1] / "fixtures" / "kavita" / "openapi" / "pinned.json"
HOST = "kavita.example"
CHAPTER = ChapterRef(10101, 10100, 101, 7, 20, "01HZZZZZZZZZZZZZZZZZZZZZZ1")


def cassette(name: str) -> dict[str, Any]:
    loaded = json.loads((CASSETTES / f"{name}.json").read_text("utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def progress_schema() -> dict[str, Any]:
    schemas = json.loads(PINNED.read_text("utf-8"))["components"]["schemas"]
    return schemas["ProgressDto"]  # type: ignore[no-any-return]


@pytest.mark.parametrize("name", sorted(p.stem for p in CASSETTES.glob("*.json")))
def test_every_cassette_says_it_is_synthetic_and_names_its_source(name: str) -> None:
    note = cassette(name)["synthetic"]
    assert "Synthetic" in note and "v0.9.1.4" in note and "ProgressDto.cs" in note


@pytest.mark.parametrize("name", ["get_progress", "get_progress_none", "post_progress"])
def test_the_progress_bodies_are_the_progress_dto_of_the_pinned_openapi(name: str) -> None:
    schema = progress_schema()
    for item in cassette(name)["interactions"]:
        raw = item["response"]["body"] if name != "post_progress" else item["request"]["body"]
        body = json.loads(raw) if isinstance(raw, str) else raw
        assert set(body) <= set(schema["properties"])  # no field the DTO does not declare
        assert set(schema["required"]) <= set(body)
        assert all(isinstance(body[f], int) for f in ("chapterId", "pageNum", "volumeId"))


def replay(
    name: str, *, status: int | None = None
) -> tuple[MemberProgress, list[float], list[Any]]:
    interactions = cassette(name)["interactions"]
    seen: list[Any] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        mine = next(i for i in interactions if i["request"]["method"] == request.method)
        answer = mine["response"]
        return httpx2.Response(
            answer["status"] if status is None else status,
            content=(answer["body"] or "").encode(), headers=answer["headers"],
        )  # fmt: skip

    resolver = FakeResolver({HOST: ["192.168.1.50"]})
    lan = asyncio.run(LanTargets.declare([f"{HOST}:80"], resolver))
    fetcher = IntegrationFetcher(resolver, lan_targets=lan, transport=httpx2.MockTransport(handler))
    client = MemberKavitaClient(fetcher, f"http://{HOST}", MemberKey("synthetic-key-value-0001"))
    sleeps: list[float] = []
    return MemberProgress(client, sleep=sleeps.append), sleeps, seen


def test_a_progress_is_read_with_its_page_and_kavitas_own_stamp() -> None:
    member, _, seen = replay("get_progress")
    got = member.get(CHAPTER)
    assert (got.page, got.modified) == (7, "2026-11-15T11:58:00.1234567Z")
    assert seen[0].url.query == b"chapterId=10101"
    assert seen[0].headers["x-api-key"] == "synthetic-key-value-0001"  # the key is a header


def test_no_progress_is_page_zero_not_an_error() -> None:
    member, _, _ = replay("get_progress_none")
    got = member.get(ChapterRef(10102, 10100, 101, 7, 20, CHAPTER.carrier_id))
    assert got.page == 0


def test_the_write_is_the_progress_dto_of_the_file() -> None:
    member, _, seen = replay("post_progress")
    member.save(CHAPTER, 12)
    assert json.loads(seen[0].content) == {
        "volumeId": 10100, "chapterId": 10101, "pageNum": 12, "seriesId": 101, "libraryId": 7,
    }  # fmt: skip


def test_a_throttling_kavita_is_waited_for_a_bounded_number_of_times() -> None:
    member, sleeps, seen = replay("throttled")
    with pytest.raises(RemoteUnavailable):
        member.get(CHAPTER)
    assert len(seen) == 3 and sleeps == [2.0, 2.0]


def test_a_failing_kavita_is_retried_with_a_growing_wait_then_given_up() -> None:
    member, sleeps, seen = replay("unavailable")
    with pytest.raises(RemoteUnavailable):
        member.get(CHAPTER)
    assert len(seen) == 3 and sleeps == sorted(sleeps) and sleeps[0] < sleeps[-1]
