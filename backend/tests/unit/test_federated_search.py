"""Federated multi-source search (M3 L18, EF-SRC-005, EF-UX-011): opaque cursors nobody can forge,
grouping by Work by append only, the formats kept apart, `searched_in` exact, a failing source that
blocks no other, an honest empty state. Synthetic sources, no network."""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from typing import Any

import pytest
from shelfhold.acquisition.application.federated_search import (
    FederatedCursors,
    FederatedPage,
    FederatedSearch,
    FederatedSource,
    InvalidFederatedCursorError,
    format_of,
)

from shelfhold.sdk.source import RateLimited, SourceError
from tests.fakes_clock import ManualClock
from tests.federated_env import FakeSearcher, FakeSource, hit

pytestmark = pytest.mark.req("EF-SRC-005")

ALICE, BOB = "user-alice", "user-bob"


class Bench:
    def __init__(self, *sources: FakeSource, formats: dict[str, str] | None = None,
                 timeout_s: float = 5.0, max_walks: int = 256) -> None:  # fmt: skip
        self.clock = ManualClock(datetime(2026, 11, 12, 9, 0, tzinfo=UTC))
        self.searcher = FakeSearcher(sources)
        self.cursors = FederatedCursors(self.clock.now, max_walks=max_walks)
        self.service = FederatedSearch(self.searcher, self.cursors, timeout_s=timeout_s)
        self.sources = [
            FederatedSource(
                s.key,
                s.manifest.name,
                (formats or {}).get(s.key, format_of(s.manifest.content_kinds)),
            )
            for s in sources
        ]  # fmt: skip

    def search(
        self, query: str = "q", *, owner: str = ALICE, cursor: str | None = None
    ) -> FederatedPage:
        return asyncio.run(self.service.search(self.sources, query, owner=owner, cursor=cursor))


def titles(page: FederatedPage) -> list[str]:
    return [g.title for g in page.items]


# -- opaque cursors ----------------------------------------------------------------------------


def two_pages(key: str, prefix: str) -> FakeSource:
    return FakeSource(
        key,
        pages=[[hit(f"{prefix}1", f"{prefix} one"), hit(f"{prefix}2", f"{prefix} two")],
               [hit(f"{prefix}3", f"{prefix} three")]],
    )  # fmt: skip


def test_federated_search_opaque_cursors() -> None:
    bench = Bench(two_pages("alpha", "A"), two_pages("beta", "B"))
    first = bench.search()
    assert first.next_cursor is not None and len(titles(first)) == 4
    token = first.next_cursor
    # opaque: nothing of a source, of a position or of a query can be read in it, and it is not an
    # encoding of anything that could be written by hand
    assert len(token) >= 32 and token.isascii()
    assert "alpha" not in token and "beta" not in token
    padded = token + "=" * (-len(token) % 4)
    try:
        decoded = base64.urlsafe_b64decode(padded)
        json.loads(decoded)
    except ValueError:
        pass
    else:  # pragma: no cover - the token must not be a JSON document
        raise AssertionError("the cursor decodes to JSON")
    assert bench.search().next_cursor != token  # a walk of its own each time: not derived
    second = bench.search(cursor=token)
    assert titles(second) == ["A three", "B three"] and second.next_cursor is None


def test_federated_cursor_cannot_be_forged() -> None:
    """The attack: a cursor written by hand, copied, altered or cut, never reaches a source."""
    bench = Bench(two_pages("alpha", "A"), two_pages("beta", "B"))
    real = bench.search().next_cursor
    assert real is not None
    calls = bench.searcher.total_calls
    hand_made = base64.urlsafe_b64encode(json.dumps({"alpha": "1", "beta": "1"}).encode()).decode()
    forged = [
        "", " ", "x", "0", "null", "../../etc/passwd", "alpha", hand_made, hand_made.rstrip("="),
        real[:-1], real + "x", real[1:], real[::-1],
        real[:-1] + ("A" if real[-1] != "A" else "B"),
        "A" * 100_000, "é" * 40, "\x00" * 40, "%s" * 20,
    ]  # fmt: skip
    for token in forged:
        with pytest.raises(InvalidFederatedCursorError):
            bench.search(cursor=token)
    assert bench.searcher.total_calls == calls  # not one source was asked
    # and the real one still works: refusing the forgeries took nothing from it
    assert titles(bench.search(cursor=real)) == ["A three", "B three"]


def test_federated_cursor_is_bound_to_its_query_and_its_owner() -> None:
    bench = Bench(two_pages("alpha", "A"))
    token = bench.search("tolkien", owner=ALICE).next_cursor
    assert token is not None
    calls = bench.searcher.total_calls
    with pytest.raises(InvalidFederatedCursorError):
        bench.search("another query", owner=ALICE, cursor=token)  # not to read another search
    with pytest.raises(InvalidFederatedCursorError):
        bench.search("tolkien", owner=BOB, cursor=token)  # not to read another person's walk
    with pytest.raises(InvalidFederatedCursorError):
        bench.search("TOLKIEN", owner=ALICE, cursor=token)  # the query is the one that was asked
    assert bench.searcher.total_calls == calls
    assert titles(bench.search("tolkien", owner=ALICE, cursor=token)) == ["A three"]


def test_federated_cursor_expires() -> None:
    bench = Bench(two_pages("alpha", "A"))
    token = bench.search().next_cursor
    assert token is not None
    bench.clock.advance(minutes=14)
    assert titles(bench.search(cursor=token)) == ["A three"]  # still within its life
    token = bench.search().next_cursor
    assert token is not None
    bench.clock.advance(minutes=16)
    with pytest.raises(InvalidFederatedCursorError):
        bench.search(cursor=token)


def test_federated_cursor_store_is_bounded() -> None:
    bench = Bench(two_pages("alpha", "A"), max_walks=3)
    oldest = bench.search().next_cursor
    assert oldest is not None
    for _ in range(3):
        bench.search()  # a flood of searches that are never continued
    assert len(bench.cursors) <= 3
    with pytest.raises(InvalidFederatedCursorError):
        bench.search(cursor=oldest)


def test_federated_cursor_replay_gives_the_same_page() -> None:
    bench = Bench(two_pages("alpha", "A"), two_pages("beta", "B"))
    token = bench.search().next_cursor
    assert token is not None
    again = bench.search(cursor=token)
    assert titles(bench.search(cursor=token)) == titles(again)  # a retried request, same answer


# -- grouping by Work, by append only ---------------------------------------------------------


@pytest.mark.req("EF-SRC-005")
def test_grouping_by_work_append_only() -> None:
    """Two sources that give the same external identifier: one Work, two Editions."""
    a = FakeSource("alpha", pages=[[hit("a1", "The Hobbit", isbn="9780261102217")]])
    b = FakeSource("beta", pages=[[hit("b9", "Der Hobbit", isbn="9780261102217")]])
    page = Bench(a, b).search()
    (group,) = page.items
    assert [(e.source_key, e.remote_id) for e in group.editions] == [
        ("alpha", "a1"),
        ("beta", "b9"),
    ]
    assert group.title == "The Hobbit"  # the first one given: it is never rewritten
    assert group.external_ids == (("isbn", "9780261102217"),)


def test_grouping_never_on_title_alone() -> None:
    a = FakeSource("alpha", pages=[[hit("a1", "Berserk"), hit("a2", "Berserk", isbn="1")]])
    b = FakeSource("beta", pages=[[hit("b1", "Berserk"), hit("b2", "berserk ", isbn="2")]])
    page = Bench(a, b).search()
    assert len(page.items) == 4  # four results, one title, four Works
    assert all(len(g.editions) == 1 for g in page.items)


def test_grouping_ignores_case_and_blanks_of_the_identifier() -> None:
    a = FakeSource("alpha", pages=[[hit("a1", "One", ISBN="978-X")]])
    b = FakeSource("beta", pages=[[hit("b1", "Two", isbn=" 978-X ")]])
    assert len(Bench(a, b).search().items) == 1


def test_grouping_never_merges_two_groups_a_result_bridges() -> None:
    """Append only: a result with the ids of two Works joins the first and merges nothing."""
    a = FakeSource("alpha", pages=[[hit("a1", "W one", isbn="1"), hit("a2", "W two", asin="2")]])
    b = FakeSource("beta", pages=[[hit("b1", "Bridge", isbn="1", asin="2")]])
    page = Bench(a, b).search()
    assert len(page.items) == 2
    first, second = page.items
    assert [e.remote_id for e in first.editions] == ["a1", "b1"]
    assert [e.remote_id for e in second.editions] == ["a2"]


def test_grouping_adds_each_edition_once() -> None:
    a = FakeSource("alpha", pages=[[hit("a1", "One", isbn="1"), hit("a1", "One", isbn="1")]])
    (group,) = Bench(a).search().items
    assert len(group.editions) == 1


def test_grouping_without_reordering_between_two_pages() -> None:
    a = FakeSource(
        "alpha", pages=[[hit("a1", "First", isbn="1"), hit("a2", "Second", isbn="2")],
                        [hit("a3", "Third", isbn="3")]],
    )  # fmt: skip
    b = FakeSource(
        "beta", pages=[[hit("b0", "Elsewhere", isbn="0")], [hit("b2", "Second again", isbn="2")]]
    )
    bench = Bench(a, b)
    first = bench.search()
    assert first.next_cursor is not None
    ids_before = [(g.group_id, g.title) for g in first.items]
    assert [t for _, t in ids_before] == ["First", "Second", "Elsewhere"]
    second = bench.search(cursor=first.next_cursor)
    # the Edition that arrives later is added to its Work, which is named again, with that
    # Edition alone; the new Work comes after the others; no Work moved
    by_id = {g.group_id: g for g in second.items}
    old_second = first.items[1].group_id
    assert [e.remote_id for e in by_id[old_second].editions] == ["b2"]
    assert by_id[old_second].appended is True
    (fresh,) = [g for g in second.items if not g.appended]
    assert fresh.title == "Third" and fresh.group_id not in {g for g, _ in ids_before}
    assert [(g.group_id, g.title) for g in first.items] == ids_before  # what was served, untouched
    # the walk, read in one piece, is the same as the pages one after the other
    assert [g.group_id for g in first.items] == ["g1", "g2", "g3"] and fresh.group_id == "g4"


def test_grouping_survives_hostile_memos() -> None:
    from shelfhold.sdk.refs import SeriesRef

    hostile: list[Any] = [
        [["isbn", "1"]], {"isbn": ["1"]}, {"isbn": None}, {"": "x"}, {"isbn": ""}, "isbn:1",
        {f"k{i}": "v" for i in range(500)}, {"isbn": "x" * 10_000}, 7, None,
    ]  # fmt: skip

    class Hostile(FakeSource):
        async def answer(self, q: str, cursor: str | None) -> Any:
            page = await super().answer(q, cursor)
            for index, memo in enumerate(hostile):
                page.items.append(
                    SeriesRef(source_key=self.key, remote_id=f"h{index}", title="Hostile",
                              memo={"external_ids": memo})
                )  # fmt: skip
            return page

    page = Bench(Hostile("alpha", pages=[[]])).search()
    assert page.state == "results"
    assert all(len(g.external_ids) <= 8 for g in page.items)
    assert all(len(v) <= 200 for g in page.items for _, v in g.external_ids)


# -- format separation -------------------------------------------------------------------------


def test_formats_are_kept_apart() -> None:
    novel = FakeSource(
        "gutenberg", kinds=("text",), pages=[[hit("n1", "Dracula", gutenberg="345")]]
    )
    scans = FakeSource("scans", kinds=("images",), pages=[[hit("s1", "Dracula", gutenberg="345")]])
    files = FakeSource("opds", kinds=("file",), pages=[[hit("f1", "Dracula", gutenberg="345")]])
    page = Bench(novel, scans, files).search()
    (group,) = page.items  # one Work...
    assert {e.format for e in group.editions} == {"text", "images", "file"}
    # ...whose Editions are never mixed from one format to another
    assert {fmt: [e.source_key for e in eds] for fmt, eds in group.editions_by_format.items()} == {
        "text": ["gutenberg"], "images": ["scans"], "file": ["opds"],
    }  # fmt: skip
    assert page.formats == ("file", "images", "text")


def test_format_of_a_source_is_its_content_kind() -> None:
    assert format_of({"images"}) == "images"
    assert format_of({"text"}) == "text"
    assert format_of({"file"}) == "file"
    assert format_of({"text", "images"}) == "mixed"
    assert format_of(set()) == "unknown"


# -- Searched in..., a failing source -----------------------------------------------------------


@pytest.mark.req("EF-SRC-005", "EF-UX-011")
def test_searched_in_lists_sources() -> None:
    a = FakeSource("alpha", name="Alpha books", pages=[[hit("a1", "One")]])
    b = FakeSource("beta", name="Beta texts", kinds=("images",), pages=[[]])
    page = Bench(a, b).search()
    assert [e.as_dict() for e in page.searched_in] == [
        {"source_key": "alpha", "name": "Alpha books", "format": "text", "status": "ok",
         "code": None, "returned": 1, "trust": {}},
        {"source_key": "beta", "name": "Beta texts", "format": "images", "status": "ok",
         "code": None, "returned": 0, "trust": {}},
    ]  # fmt: skip


def test_searched_in_stays_whole_and_counts_across_pages() -> None:
    a = two_pages("alpha", "A")
    b = FakeSource("beta", pages=[[hit("b1", "B one")]])  # done after the first page
    bench = Bench(a, b)
    first = bench.search()
    assert first.next_cursor is not None
    second = bench.search(cursor=first.next_cursor)
    assert [(e.source_key, e.returned) for e in first.searched_in] == [("alpha", 2), ("beta", 1)]
    assert [(e.source_key, e.returned) for e in second.searched_in] == [("alpha", 3), ("beta", 1)]
    assert b.calls == [("q", None)]  # a source that is exhausted is not asked again


def test_source_error_does_not_block_others() -> None:
    ok = FakeSource("alpha", pages=[[hit("a1", "Found")]])
    limited = FakeSource("beta", error=RateLimited("slow down"))
    broken = FakeSource("gamma", error=RuntimeError("boom at https://secret.example/path?k=v"))
    page = Bench(ok, limited, broken).search()
    assert titles(page) == ["Found"] and page.state == "results"
    statuses = {e.source_key: (e.status, e.code) for e in page.searched_in}
    assert statuses == {
        "alpha": ("ok", None),
        "beta": ("error", "source.rate_limited"),
        "gamma": ("error", "source.error"),  # an unknown failure: a code, never its message
    }
    assert "secret.example" not in repr(page) and "boom" not in repr(page)


def test_source_with_a_typed_error_reports_its_code() -> None:
    class Gone(SourceError):
        code = "source.changed"

    page = Bench(FakeSource("alpha", error=Gone("x"))).search()
    assert [(e.status, e.code) for e in page.searched_in] == [("error", "source.changed")]


def test_slow_source_times_out_without_holding_the_others() -> None:
    fast = FakeSource("alpha", pages=[[hit("a1", "Quick")]])
    slow = FakeSource("beta", pages=[[hit("b1", "Never")]], delay=2.0)
    page = Bench(fast, slow, timeout_s=0.05).search()
    assert titles(page) == ["Quick"]
    assert {e.source_key: e.code for e in page.searched_in} == {
        "alpha": None,
        "beta": "source.timeout",
    }


def test_failed_source_is_not_asked_again_on_the_next_page() -> None:
    broken = FakeSource("beta", error=RateLimited("x"))
    bench = Bench(two_pages("alpha", "A"), broken)
    first = bench.search()
    assert first.next_cursor is not None
    second = bench.search(cursor=first.next_cursor)
    assert len(broken.calls) == 1
    assert [e.status for e in second.searched_in] == ["ok", "error"]  # and it is still listed


# -- the honest empty state --------------------------------------------------------------------


@pytest.mark.req("EF-UX-011")
def test_empty_state_lists_no_sites() -> None:
    page = Bench(FakeSource("alpha", pages=[[]]), FakeSource("beta", pages=[[]])).search()
    assert page.items == [] and page.state == "empty" and page.action == "request"
    assert [e.source_key for e in page.searched_in] == ["alpha", "beta"]  # what was asked...
    # ...and nothing to go and look at elsewhere: no address, no site, in the whole answer
    dumped = json.dumps([e.as_dict() for e in page.searched_in])
    assert "http" not in dumped and "://" not in dumped and "www" not in dumped
    assert set(page.searched_in[0].as_dict()) == {
        "source_key", "name", "format", "status", "code", "returned", "trust",
    }  # fmt: skip


@pytest.mark.req("EF-UX-011")
def test_empty_state_is_not_claimed_when_every_source_failed() -> None:
    page = Bench(
        FakeSource("alpha", error=RateLimited("x")), FakeSource("beta", error=OSError())
    ).search()
    assert page.state == "unavailable" and page.action is None  # "nothing found" would be a lie
    assert [e.status for e in page.searched_in] == ["error", "error"]


@pytest.mark.req("EF-UX-011")
def test_empty_state_without_any_source() -> None:
    page = Bench().search()
    assert page.state == "no_sources" and page.action == "request" and page.searched_in == []


@pytest.mark.req("EF-UX-011")
def test_a_later_empty_page_is_not_the_empty_state() -> None:
    a = FakeSource("alpha", pages=[[hit("a1", "One")], []])
    bench = Bench(a)
    first = bench.search()
    assert first.next_cursor is not None
    second = bench.search(cursor=first.next_cursor)
    assert second.items == [] and second.state == "results" and second.action is None
