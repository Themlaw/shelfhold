"""Transversal review of M2: three refusals that must close by default.

1. a Work purged on purpose does not come back through the identifiers a source gives for it, by
   another source or another `remote_id` (EF-BIB-053); no screen to ask means no arrival;
2. a book that joins an Edition already rated takes the age its source declares as a suggestion,
   and a shelf below 18+ does not take it above the Work's rating (EF-BIB-055, EF-BIB-037);
3. a Suwayomi sub-source that does not say `isNsfw: false` is not known to be safe (EF-SRC-021).
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa

from shelfhold.acquisition.application.tree_target import source_suggestion
from shelfhold.acquisition.public import AcquireRefused, NativeAcquirer
from shelfhold.catalog.public import (
    Catalog,
    EditionSpec,
    ExternalId,
    add_external_id,
    content_warning_age,
)
from shelfhold.sdk.age import X_MIN_AGE, AgeRating
from shelfhold.sdk.refs import SeriesRef
from tests.acquire_env import BOOK, KEY, Bench, NativeBooks, acquire_bench
from tests.follow_env import Bench as FollowBench
from tests.follow_env import add_adult_shelf, follow_bench

EIGHTEEN = AgeRating(X_MIN_AGE)
X = AgeRating(X_MIN_AGE, x=True)
OTHER = "book/43"
BOTH = ("work", "edition", "external_id", "source_item", "source_binding", "download_request")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class Screen:
    """A screen of the Work tombstones: it blocks the `(provider, value)` it is given."""

    def __init__(self, blocked: Sequence[tuple[str, str]] = ()) -> None:
        self.blocked = set(blocked)
        self.asked: list[tuple[str, str]] = []

    def places(self, places: object) -> set[str]:
        return set()

    def work_ids(self, ids: Sequence[Any]) -> set[tuple[str, str]]:
        pairs = {(i.provider, i.value) for i in ids}
        self.asked.extend(sorted(pairs))
        return pairs & self.blocked


def broken_screen() -> Any:
    raise RuntimeError("the tombstones cannot be replayed")


class UnreadableScreen(Screen):
    def work_ids(self, ids: Sequence[Any]) -> set[tuple[str, str]]:
        raise RuntimeError("the journal of the tombstones is not replayed")


def prepare(bench: Bench, remote_id: str = BOOK, **kw: Any) -> Any:
    ref = SeriesRef(source_key=KEY, remote_id=remote_id, title="Un livre de test")
    return run(bench.acquirer.prepare(ref, **kw))


def count(bench: Bench, table: str) -> int:
    return int(bench.rows(f"SELECT count(*) AS n FROM {table}")[0]["n"])  # noqa: S608


def nothing_written(bench: Bench) -> None:
    assert {t: count(bench, t) for t in BOTH} == dict.fromkeys(BOTH, 0)


# -- 1. the Work tombstones on acquisition --------------------------------------------------------


@pytest.mark.req("EF-BIB-053")
def test_a_purged_work_does_not_return_by_its_external_id_from_another_series() -> None:
    """The identifier is the one a first source gave; a second source (another `remote_id`)
    carries it again: refused, and not one row is written."""
    screen = Screen([("gutenberg", "1342")])
    plugin = NativeBooks(external_ids={"gutenberg": "1342"})
    with acquire_bench(plugin, work_screen=lambda: screen) as bench:
        with pytest.raises(AcquireRefused) as refused:
            prepare(bench, OTHER)
        assert refused.value.code == "acquire.work_tombstoned"
        nothing_written(bench)
        assert ("gutenberg", "1342") in screen.asked


@pytest.mark.req("EF-BIB-053")
def test_a_purged_work_does_not_return_by_the_key_that_groups_its_books() -> None:
    screen = Screen([("native_books_work", "the-novel")])
    with acquire_bench(NativeBooks(work_key="the-novel"), work_screen=lambda: screen) as bench:
        with pytest.raises(AcquireRefused) as refused:
            prepare(bench)
        assert refused.value.code == "acquire.work_tombstoned"
        nothing_written(bench)


@pytest.mark.req("EF-BIB-053")
def test_one_tombstoned_identifier_among_several_refuses_the_whole_arrival() -> None:
    screen = Screen([("oclc", "123")])
    plugin = NativeBooks(external_ids={"gutenberg": "1342", "oclc": "123"})
    with acquire_bench(plugin, work_screen=lambda: screen) as bench:
        with pytest.raises(AcquireRefused) as refused:
            prepare(bench)
        assert refused.value.code == "acquire.work_tombstoned"
        nothing_written(bench)


@pytest.mark.req("EF-BIB-053")
def test_an_acquisition_with_identifiers_and_no_screen_fails_closed() -> None:
    plugin = NativeBooks(external_ids={"gutenberg": "1342"})
    with acquire_bench(plugin, work_screen=None) as bench:
        with pytest.raises(AcquireRefused) as refused:
            prepare(bench)
        assert refused.value.code == "acquire.tombstones_unavailable"
        nothing_written(bench)


@pytest.mark.req("EF-BIB-053")
def test_a_screen_that_cannot_be_opened_fails_closed() -> None:
    plugin = NativeBooks(external_ids={"gutenberg": "1342"})
    with acquire_bench(plugin, work_screen=broken_screen) as bench:
        with pytest.raises(AcquireRefused) as refused:
            prepare(bench)
        assert refused.value.code == "acquire.tombstones_unavailable"
        nothing_written(bench)


@pytest.mark.req("EF-BIB-053")
def test_a_screen_that_fails_when_asked_fails_closed_and_writes_nothing() -> None:
    plugin = NativeBooks(external_ids={"gutenberg": "1342"})
    with acquire_bench(plugin, work_screen=UnreadableScreen) as bench:
        with pytest.raises(AcquireRefused) as refused:
            prepare(bench)
        assert refused.value.code == "acquire.tombstones_unavailable"
        nothing_written(bench)


@pytest.mark.req("EF-BIB-053")
def test_an_identifier_nobody_purged_is_still_remembered() -> None:
    screen = Screen([("gutenberg", "1343")])
    plugin = NativeBooks(external_ids={"gutenberg": "1342"}, work_key="the-novel")
    with acquire_bench(plugin, work_screen=lambda: screen) as bench:
        prepare(bench)
        found = {(r["provider"], r["value"]) for r in bench.rows("SELECT * FROM external_id")}
        assert found == {("gutenberg", "1342"), ("native_books_work", "the-novel")}


@pytest.mark.req("EF-BIB-053")
def test_a_source_without_identifiers_needs_no_screen() -> None:
    with acquire_bench(NativeBooks(), work_screen=None) as bench:
        assert prepare(bench).created


# -- 2. the age gate of a book that joins a rated Edition -----------------------------------------


def rated_work_then_book(bench: Bench, shelf: str, *, age: int = 10) -> Any:
    """A Work the source's key names, rated by an admin, with its one Edition (an adopted file, no
    binding yet) rated and filed on the family shelf; a book of the source then joins that very
    Edition."""
    now = datetime.now(UTC)
    with bench.lib.engine.begin() as connection:
        books = Catalog(connection)
        work_id = books.create_work("novel", "Un livre de test")
        edition = books.ensure_edition(
            EditionSpec(work_id, "en", translation_type="official", media="text", publisher="")
        )
        add_external_id(
            connection, work_id, ExternalId("work", "native_books_work", "novel"), origin="source"
        )
        connection.execute(
            sa.text("UPDATE work SET age_rating = :a, age_x = 0, rated_at = :t"),
            {"a": age, "t": now},
        )
        connection.execute(
            sa.text("UPDATE edition SET rated_at = :t, shelf_id = :s"), {"t": now, "s": shelf}
        )
    joined = prepare(bench, OTHER, shelf_id=shelf)
    assert (joined.work_id, joined.edition_id) == (work_id, edition.id)
    return joined


def shelf_of_edition(bench: Bench) -> str | None:
    (row,) = bench.rows("SELECT shelf_id FROM edition")
    return row["shelf_id"]


def suggestions(bench: Bench) -> list[tuple[int, int, str]]:
    return [
        (r["value"], r["x"], r["provenance"])
        for r in bench.rows("SELECT value, x, provenance FROM age_suggestion")
    ]


@pytest.mark.req("EF-BIB-055", "EF-BIB-037")
@pytest.mark.parametrize(
    ("warning", "expected"),
    [("mixed", (18, 0, "sub_source")), ("nsfw", (18, 1, "sub_source"))],
)
def test_a_book_joining_a_rated_edition_is_held_when_its_source_warns(
    warning: str, expected: tuple[int, int, str]
) -> None:
    with acquire_bench(NativeBooks(work_key="novel", content_warning=warning)) as bench:
        shelf = add_adult_shelf(bench.lib, max_age_rating=13)
        rated_work_then_book(bench, shelf)
        assert expected in suggestions(bench)
        assert shelf_of_edition(bench) is None  # "À classer", never on the family shelf


@pytest.mark.req("EF-BIB-055", "EF-BIB-037")
def test_a_book_of_a_safe_source_joining_a_rated_edition_is_filed() -> None:
    with acquire_bench(NativeBooks(work_key="novel", content_warning="safe")) as bench:
        shelf = add_adult_shelf(bench.lib, max_age_rating=13)
        rated_work_then_book(bench, shelf)
        assert suggestions(bench) == []
        assert shelf_of_edition(bench) == shelf


@pytest.mark.req("EF-BIB-055", "EF-BIB-037")
@pytest.mark.parametrize("warning", [None, "", "  ", "unknown", "NSFW?"])
def test_an_unknown_or_missing_warning_counts_as_18_plus(warning: str | None) -> None:
    """The registry refuses a manifest with such a value; the sub-source row of a book can still
    hold one (NULL, or a word of another release): it is not known to be safe."""
    with acquire_bench(NativeBooks()) as bench, bench.lib.engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO sub_source (plugin_id, source_key, name, content_warning)"
                " VALUES (:p, :k, 'Sub', :w)"
            ),
            {"p": KEY, "k": f"{KEY}:sub", "w": warning},
        )
        ref = SeriesRef(source_key=f"{KEY}:sub", remote_id="x", title="t")
        record = SimpleNamespace(key=KEY)
        manifest = bench.plugin.manifest
        found = NativeAcquirer._declared_warning(connection, record, manifest, ref)
        assert found == warning
        assert content_warning_age(found) == EIGHTEEN
        missing = SeriesRef(source_key=f"{KEY}:nobody", remote_id="x", title="t")
        nothing = NativeAcquirer._declared_warning(connection, record, manifest, missing)
        assert nothing is None and content_warning_age(nothing) == EIGHTEEN


@pytest.mark.req("EF-BIB-055", "EF-BIB-037")
@pytest.mark.parametrize("warning", [" MIXED ", "Mixed", "NSFW", " Nsfw\n"])
def test_the_case_and_the_spaces_of_the_warning_do_not_open_the_shelf(warning: str) -> None:
    assert content_warning_age(warning) is not None


@pytest.mark.req("EF-BIB-055", "EF-BIB-037")
def test_a_suggestion_not_above_the_rating_of_the_work_keeps_the_book_on_its_shelf() -> None:
    """The adult shelf has no ceiling: the suggestion is recorded, nothing is held."""
    with acquire_bench(NativeBooks(work_key="novel", content_warning="mixed")) as bench:
        shelf = add_adult_shelf(bench.lib)
        rated_work_then_book(bench, shelf, age=18)
        assert (18, 0, "sub_source") in suggestions(bench)
        assert shelf_of_edition(bench) == shelf


# -- 3. one rule for the warning of a source ------------------------------------------------------


@pytest.mark.req("EF-SRC-021", "EF-BIB-055")
@pytest.mark.parametrize(
    ("warning", "expected"),
    [
        ("safe", None), (" SAFE ", None), ("Safe\t", None),
        ("mixed", EIGHTEEN), (" Mixed", EIGHTEEN), ("MIXED\n", EIGHTEEN),
        ("nsfw", X), (" NSFW ", X), ("Nsfw", X),
        (None, EIGHTEEN), ("", EIGHTEEN), ("   ", EIGHTEEN), ("unsafe", EIGHTEEN),
        ("sa fe", EIGHTEEN), ("null", EIGHTEEN),
    ],
)  # fmt: skip
def test_content_warning_age_folds_and_fails_closed(
    warning: str | None, expected: AgeRating | None
) -> None:
    assert content_warning_age(warning) == expected  # type: ignore[arg-type]
    assert source_suggestion(warning) == expected  # the import of a tree has no rule of its own


def set_nsfw(bench: FollowBench, value: object, *, drop: bool = False) -> None:
    for source in bench.server.state["sources"]:
        if drop:
            source.pop("isNsfw", None)
        else:
            source["isNsfw"] = value


def stored_warnings(bench: FollowBench) -> set[str]:
    return {r["content_warning"] for r in bench.rows("SELECT content_warning FROM sub_source")}


@pytest.mark.req("EF-SRC-021", "EF-BIB-055")
@pytest.mark.parametrize(
    ("value", "drop", "expected"),
    [
        (None, True, (18, 0)),  # absent
        (None, False, (18, 0)),  # null
        ("false", False, (18, 0)),  # a string is not a boolean
        (0, False, (18, 0)),
        (True, False, (18, 1)),
    ],
)
def test_a_suwayomi_source_that_does_not_say_it_is_safe_is_not_safe(
    value: object, drop: bool, expected: tuple[int, int]
) -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        set_nsfw(bench, value, drop=drop)
        bench.run()
        found = bench.rows("SELECT value, x, provenance FROM age_suggestion")
        assert found and all((s["value"], s["x"]) == expected for s in found)
        assert all(s["provenance"] == "sub_source" for s in found)


@pytest.mark.req("EF-SRC-021")
def test_a_suwayomi_source_that_says_false_is_safe_and_suggests_nothing() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        set_nsfw(bench, False)
        bench.run()
        assert bench.rows("SELECT count(*) AS n FROM age_suggestion")[0]["n"] == 0
        warnings = {
            r["content_warning"] for r in bench.rows("SELECT content_warning FROM sub_source")
        }
        assert warnings == {"safe"}


@pytest.mark.req("EF-SRC-021")
def test_the_stored_warning_of_an_unknown_nsfw_is_mixed() -> None:
    with follow_bench(seeded=False, adult_shelf=True) as bench:
        set_nsfw(bench, None, drop=True)
        bench.run()
        warnings = {
            r["content_warning"] for r in bench.rows("SELECT content_warning FROM sub_source")
        }
        assert warnings == {"mixed"}
