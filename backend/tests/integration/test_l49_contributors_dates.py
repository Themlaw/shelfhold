"""L49: authors and partial dates, from the followed series to the ComicInfo of the written CBZ.

Every test goes through the real chain: the follow cycle against the simulated Suwayomi, the
`ExportPipeline`, then the ComicInfo read back from the CBZ on the shelf. No row is written to
fake the state under proof (EF-BIB-046, EF-BIB-047).
"""

import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from typing import Any

import pytest
import sqlalchemy as sa
import structlog.testing

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from shelfhold.plugin_host.adapters.meta.suwayomi import SuwayomiService
from shelfhold.sdk.fetcher import Fetcher
from shelfhold.sdk.refs import SeriesRef, UnitInfo
from tests.export_env import export_env_on
from tests.fakes.suwayomi import BASE_URL
from tests.follow_env import Bench, follow_bench, new_chapter
from tests.integration.test_follow_policy import _settle, _watermark
from tests.integration.test_l45_upstream import _comicinfo_of

NOW = datetime(2026, 11, 5, 9, 0, tzinfo=UTC)


def _export_chapter(bench: Bench) -> tuple[ET.Element, list[dict[str, Any]]]:
    """Follow chapter 3 of the series, write it to the shelf, read its ComicInfo back."""
    env = export_env_on(bench.lib)
    with bench.lib.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == bench.edition_id)
            .values(shelf_id=env.shelf_id)
        )
    _watermark(bench, "2")
    chapter = bench.alpha()["chapters"]
    if not any(c["chapterNumber"] == 3.0 for c in chapter):
        chapter.append(new_chapter(5030, 3))
    bench.run()
    _settle(bench)
    with structlog.testing.capture_logs() as logs:
        env.pipeline.run(env.destination_id, job_id=new_ulid())
    exported = [p for p in env.files() if p.name.endswith(".cbz") and "Ch.3" in p.name]
    assert len(exported) == 1, [p.name for p in env.files()]
    return _comicinfo_of(exported[0]), logs


def _unit_dates(bench: Bench) -> dict[str, Any]:
    return bench.rows(
        "SELECT published_on, date_precision, published_at FROM unit "
        "WHERE edition_id = :e AND number_norm = '3'",
        e=bench.edition_id,
    )[0]


@pytest.mark.req("EF-BIB-046")
def test_inverted_author_from_suwayomi_exported_as_natural_order() -> None:
    with follow_bench() as bench:
        bench.alpha()["author"] = "Doe, Jane"
        bench.alpha()["artist"] = "Smith, John"
        info, _ = _export_chapter(bench)
    assert info.findtext("Writer") == "Jane Doe"
    assert info.findtext("Penciller") == "John Smith"


@pytest.mark.req("EF-BIB-046")
def test_comma_in_org_replaced_and_logged() -> None:
    with follow_bench() as bench:
        bench.alpha()["author"] = "Éditeur, Ltd."
        bench.alpha()["artist"] = ""
        info, logs = _export_chapter(bench)
    assert info.findtext("Writer") == "Éditeur Ltd."
    assert info.find("Penciller") is None
    warned = [e for e in logs if e["event"] == "export.comicinfo_warning"]
    assert any("comma replaced" in str(e["detail"]) for e in warned), logs


@pytest.mark.req("EF-BIB-046", "ENF-SEC-015")
def test_hostile_author_sanitized_in_base_and_comicinfo() -> None:
    with follow_bench() as bench:
        bench.alpha()["author"] = "<script>alert(1)</script>Jane Doe‮"
        bench.alpha()["artist"] = ""
        info, _ = _export_chapter(bench)
        stored = bench.rows("SELECT primary_name FROM contributor")
    assert [r["primary_name"] for r in stored] == ["alert(1)Jane Doe"] or all(
        "<" not in r["primary_name"] and "‮" not in r["primary_name"] for r in stored
    )
    assert stored, "the author was not ingested"
    writer = info.findtext("Writer") or ""
    assert writer and "<" not in writer and "‮" not in writer and "script" not in writer


@pytest.mark.req("EF-BIB-046")
def test_authors_ingested_once_and_shared_between_series() -> None:
    with follow_bench() as bench:
        bench.alpha()["author"] = "Doe, Jane"
        bench.alpha()["artist"] = "Doe, Jane"
        bench.run()
        bench.run()
        assert len(bench.rows("SELECT id FROM contributor")) == 1
        roles = bench.rows("SELECT role FROM contribution ORDER BY role")
    assert [r["role"] for r in roles] == ["art", "aut"]


class _PartialDates(SuwayomiService):
    """A service whose source only knows the year of a release."""

    def __init__(self, published_on: str) -> None:
        super().__init__(BASE_URL, now=lambda: NOW)
        self._published_on = published_on

    async def list_units(self, f: Fetcher, ref: SeriesRef) -> list[UnitInfo]:
        units = await super().list_units(f, ref)
        return [
            u.model_copy(
                update={
                    "published_at": None,
                    "published_on": self._published_on,
                    "date_precision": {4: "year", 7: "month", 10: "day"}[len(self._published_on)],
                }
            )
            for u in units
        ]


@pytest.mark.req("EF-BIB-047")
def test_upload_date_year_only_writes_year_only() -> None:
    with follow_bench(service=_PartialDates("2023")) as bench:
        info, _ = _export_chapter(bench)
        dates = _unit_dates(bench)
    assert (dates["published_on"], dates["date_precision"]) == ("2023", "year")
    assert info.findtext("Year") == "2023"
    assert info.find("Month") is None and info.find("Day") is None


@pytest.mark.req("EF-BIB-047")
def test_upload_date_month_only_writes_year_and_month() -> None:
    with follow_bench(service=_PartialDates("2023-05")) as bench:
        info, _ = _export_chapter(bench)
    assert (info.findtext("Year"), info.findtext("Month")) == ("2023", "5")
    assert info.find("Day") is None


@pytest.mark.req("EF-BIB-047")
def test_full_upload_date_writes_the_day() -> None:
    with follow_bench() as bench:  # 1760000000000 ms = 2025-10-09
        info, _ = _export_chapter(bench)
        dates = _unit_dates(bench)
    assert (dates["published_on"], dates["date_precision"]) == ("2025-10-09", "day")
    assert [info.findtext(k) for k in ("Year", "Month", "Day")] == ["2025", "10", "9"]


@pytest.mark.req("EF-BIB-047")
def test_zero_upload_date_never_stored() -> None:
    with follow_bench() as bench:
        zero = new_chapter(5030, 3)
        zero["uploadDate"] = "0"
        bench.alpha()["chapters"].append(zero)
        info, _ = _export_chapter(bench)
        dates = _unit_dates(bench)
    assert dates == {"published_on": None, "date_precision": None, "published_at": None}
    assert all(info.find(k) is None for k in ("Year", "Month", "Day"))


@pytest.mark.req("EF-BIB-047", "ENF-TIME-001")
def test_partial_date_in_the_future_not_believed() -> None:
    with follow_bench(service=_PartialDates("2099")) as bench:
        info, _ = _export_chapter(bench)
        dates = _unit_dates(bench)
    assert dates["published_on"] is None
    assert info.find("Year") is None
