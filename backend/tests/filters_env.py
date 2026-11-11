"""Helpers of the L28 tests: download filters and numbering gaps on the multi-source bench."""

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import timedelta
from typing import Any

import sqlalchemy as sa

from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from shelfhold.sdk.source import RateLimited
from tests.fakes.suwayomi import BASE_URL
from tests.follow_env import START, Bench, follow_bench
from tests.multi_source_env import ALPHA, bind_beta, watermark

Edit = Callable[[UnitInfo], UnitInfo]


class Stamped:
    """The real Suwayomi adapter, with what a source can say that Suwayomi does not (a language,
    an extra marker) added to the listing of one series (by its remote id)."""

    def __init__(self, inner: SuwayomiService, edits: dict[str, Edit]) -> None:
        self._inner = inner
        self.edits = edits
        self.failing: set[str] = set()  # series whose listing the source refuses (rate limit)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def list_units(self, fetcher: Any, ref: SeriesRef) -> list[UnitInfo]:
        if ref.remote_id in self.failing:
            raise RateLimited("slow down")
        units = await self._inner.list_units(fetcher, ref)
        edit = self.edits.get(ref.remote_id)
        return [edit(unit) for unit in units] if edit else list(units)


def listing(series: int, numbers: list[float], team: str = "A") -> list[dict[str, Any]]:
    """Chapters of a series of the simulated server; a decimal number is a chapter like 10.5."""
    prefix, base = ("/series-alpha", 6000) if series == ALPHA else ("/series-beta", 7000)
    stamp = str(int((START - timedelta(days=1)).timestamp() * 1000))
    return [
        {
            "id": base + int(number * 10) * 10 + ord(team) - 64,
            "url": f"{prefix}/ch-{number:07.2f}-{team}", "name": f"Chapter {number:g}",
            "chapterNumber": float(number), "sourceOrder": index, "scanlator": f"Team {team}",
            "uploadDate": stamp, "cbz": "comic_a_001.cbz" if series == ALPHA else "comic_a_002.cbz",
        }
        for index, number in enumerate(numbers)
    ]  # fmt: skip


def lang_of(tag: str) -> Edit:
    return lambda unit: unit.model_copy(update={"lang": tag})


def extra_when(number: str) -> Edit:
    """The source marks the chapter `number` as an extra (its raw number says so)."""
    return lambda unit: (
        unit.model_copy(update={"number": f"{unit.number} extra"})
        if unit.number == number
        else unit
    )


@contextmanager
def filter_bench(
    edits: dict[str, Edit] | None = None, *, two_bindings: bool = True, **knobs: Any
) -> Iterator[Bench]:
    now: list[Any] = []
    inner = SuwayomiService(BASE_URL, now=lambda: now[0].now() if now else START)
    service = Stamped(inner, edits or {})
    with follow_bench(service=service, **knobs) as bench:
        now.append(bench.clock)
        bench.failing = service.failing  # type: ignore[attr-defined]
        if two_bindings:
            bind_beta(bench)
        watermark(bench)
        yield bench


def set_filters(bench: Bench, filters: dict[str, Any] | None, *, cap: int | None = None) -> None:
    with bench.lib.engine.begin() as connection:
        connection.execute(
            sa.text("UPDATE series_follow SET filters = :f, cap = :c"),
            {"f": None if filters is None else json.dumps(filters), "c": cap},
        )


def asked(bench: Bench) -> list[int]:
    """Every chapter the simulated server was ever asked for (its own counter)."""
    return list(bench.server.enqueue_calls)


def reasons(bench: Bench, *numbers: str) -> dict[str, str | None]:
    """The reason shown on each Unit of these numbers (None: not filtered)."""
    return {
        r["n"]: r["reason"]
        for r in bench.rows(
            "SELECT number_norm AS n, filtered_reason AS reason FROM unit "
            "WHERE track <> 'unnumbered' ORDER BY number_sort"
        )
        if r["n"] in numbers
    }


def gaps(bench: Bench) -> list[tuple[str, str, str]]:
    return [
        (r["track"], r["number_segment"], r["number_norm"])
        for r in bench.rows(
            "SELECT track, number_segment, number_norm FROM series_gap "
            "ORDER BY track, number_segment, CAST(number_norm AS REAL)"
        )
    ]
