"""M2 wiring (L26, EF-BIB-044): the adoption cycle reads the ComicInfo of an adopted volume and
puts the chapters it names under that volume (origin `comicinfo`). `shelfhold adopt` runs against
the simulated Kavita; nothing here writes a `unit_coverage` row by hand. A hostile ComicInfo
(a range past the bound, a volume that is not the file's own, a range another file already
holds, look-alike digits) writes nothing and never stops the adoption."""

from __future__ import annotations

from pathlib import Path

import pytest

from shelfhold.config import Settings
from tests.l54_env import Bench54, bench54
from tests.library_env import cbz_bytes

pytestmark = pytest.mark.req("EF-BIB-044")

SERIES = "Roman Alpha"


def info(volume: str, number: str, kind: str = "Volume") -> bytes:
    body = f"<Series>{SERIES}</Series><LanguageISO>fr</LanguageISO>"
    body += f"<{kind}>{volume}</{kind}><Number>{number}</Number>" if volume else ""
    return f'<?xml version="1.0" encoding="utf-8"?><ComicInfo>{body}</ComicInfo>'.encode()


def adopt_volumes(
    b: Bench54, capsys: pytest.CaptureFixture[str], files: dict[str, bytes | None]
) -> None:
    directory = b.env.inst.library / "Kavita" / SERIES
    directory.mkdir(parents=True, exist_ok=True)
    remote = []
    for index, (name, comicinfo) in enumerate(files.items()):
        pages = ((f"{index}.png", b"\x89PNG\r\n\x1a\n" + name.encode()),)
        (directory / name).write_bytes(cbz_bytes(pages, comicinfo=comicinfo))
        remote.append(f"/manga/Kavita/{SERIES}/{name}")
    b.env.inst.kavita.add_series(
        SERIES, f"/manga/Kavita/{SERIES}", remote, library_id=int(b.library_ref)
    )
    assert b.adopt(capsys) == 0


def under(b: Bench54, volume: str) -> list[str]:
    """The chapter numbers an adopted volume holds, and where that came from."""
    rows = b.rows(
        "SELECT u.number_norm AS n, c.origin AS o FROM unit_coverage c "
        "JOIN unit u ON u.id = c.unit_id JOIN export_item i ON i.id = c.carrier_id "
        "WHERE c.carrier_type = 'export_item' AND u.track = 'main' "
        "AND json_extract(i.frozen_attrs, '$.number_norm') = :v",
        v=volume,
    )
    assert {r["o"] for r in rows} <= {"comicinfo"}
    return sorted((r["n"] for r in rows), key=int)


def test_adoption_reads_the_range_of_each_volume_from_its_comicinfo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        adopt_volumes(b, capsys, {
            f"{SERIES} - Vol.1.cbz": info("1", "1-5"), f"{SERIES} - Vol.2.cbz": info("2", "6-10"),
        })  # fmt: skip
        assert under(b, "1") == ["1", "2", "3", "4", "5"]
        assert under(b, "2") == ["6", "7", "8", "9", "10"]
        assert "(coverage_unknown)" not in b.status(capsys)  # nothing is left to ask the admin


@pytest.mark.parametrize(
    "hostile",
    [
        "1-100000",  # a typo that would fill the table
        "5-1",
        "1-",
        "-5",
        "1-2-3",
        "0x1-0x5",
        "1e3-1e4",
        "1 - 5 6",
        chr(0xFF11) + "-" + chr(0xFF15),  # full-width digits
        chr(0x661) + "-" + chr(0x665),  # Arabic-Indic digits
        "1-" + "9" * 400,
    ],
)
def test_a_hostile_range_writes_nothing_and_the_adoption_goes_on(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    hostile: str,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        adopt_volumes(b, capsys, {f"{SERIES} - Vol.1.cbz": info("1", hostile)})
        assert under(b, "1") == []
        assert b.rows("SELECT count(*) AS n FROM export_item")[0]["n"] == 1  # adopted all the same
        assert "(coverage_unknown)" in b.status(capsys)  # the admin is asked, nothing is guessed


def test_a_file_cannot_claim_the_range_of_another_volume(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        adopt_volumes(b, capsys, {
            f"{SERIES} - Vol.1.cbz": info("2", "6-10"),  # Vol.1 says it is volume 2
            f"{SERIES} - Vol.2.cbz": info("2", "6-10"),
        })  # fmt: skip
        assert under(b, "1") == []
        assert under(b, "2") == ["6", "7", "8", "9", "10"]  # only its own file puts them there


def test_a_range_another_file_already_holds_is_refused_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        adopt_volumes(b, capsys, {
            f"{SERIES} - Vol.1.cbz": info("1", "1-5"),
            f"{SERIES} - Vol.2.cbz": info("2", "3-8"),  # overlaps volume 1
        })  # fmt: skip
        assert under(b, "1") == ["1", "2", "3", "4", "5"]
        assert under(b, "2") == []  # nothing of it: two files never share a chapter


def test_a_chapter_file_never_takes_a_range_from_its_comicinfo(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        adopt_volumes(b, capsys, {
            f"{SERIES} - Ch.3.cbz": info("1", "1-5"), f"{SERIES} - Vol.1.cbz": None,
        })  # fmt: skip
        assert under(b, "1") == []


@pytest.mark.parametrize("volume", ["1e999999", "\uff11", "Infinity", "NaN", "-1", "1/2", "1" * 50])
def test_a_hostile_volume_number_is_no_volume_and_costs_nothing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
    volume: str,
) -> None:
    with bench54(monkeypatch, settings, tmp_path) as b:
        adopt_volumes(b, capsys, {f"{SERIES} - Vol.1.cbz": info(volume, "1-5")})
        assert under(b, "1") == []
        assert b.rows("SELECT count(*) AS n FROM export_item")[0]["n"] == 1


def test_a_range_wider_than_the_cap_is_proposed_in_the_preview_and_never_applied(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Review of #172: a ComicInfo is trusted for 200 chapters, not for 2000."""
    from shelfhold.export.application.coverage import (
        ADOPTED_RANGE_CAP,
        CoverageError,
        apply_comicinfo_range,
        comicinfo_range_too_wide,
    )

    assert ADOPTED_RANGE_CAP == 200
    wide = {"Volume": "1", "Number": "1-1000"}
    assert comicinfo_range_too_wide(wide)
    assert not comicinfo_range_too_wide({"Volume": "1", "Number": "1-200"})
    assert comicinfo_range_too_wide({"Volume": "1", "Number": "1-201"})
    with pytest.raises(CoverageError):  # refused before any read of the database
        apply_comicinfo_range(None, "e", wide)  # type: ignore[arg-type]
    with bench54(monkeypatch, settings, tmp_path) as b:
        adopt_volumes(b, capsys, {f"{SERIES} - Vol.1.cbz": info("1", "1-1000")})
        assert under(b, "1") == []
        assert b.rows("SELECT count(*) AS n FROM export_item")[0]["n"] == 1
