"""L45: upstream text and dates, followed through the real cycle (ENF-SEC-015, ENF-TIME-001).

The simulated Suwayomi serves a hostile chapter; the real follow cycle ingests and downloads it,
the real export pipeline writes the CBZ, and what Kavita would read (the ComicInfo) is checked.
"""

import io
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

import pytest
import sqlalchemy as sa
import structlog.testing

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from tests.export_env import export_env_on
from tests.follow_env import Bench, follow_bench, new_chapter
from tests.integration.test_follow_policy import _chapter, _serve, _settle, _watermark

HOSTILE_TITLES = [
    "<script>alert(1)</script>Chapter 3",
    "Chapter 3 <img src=x onerror=alert(1)>",
    "&lt;script&gt;alert(1)&lt;/script&gt;Chapter 3",
    "Chapter 3 ‮evil‬",
    "Chapter\x07 3\x00\x1b[31m",
    '<a href="javascript:alert(1)">Chapter 3</a>',
]
HOSTILE_TEAMS = [
    "<b>Team</b><script>alert(1)</script>",
    "Team ‮evil",
    "Team\x07\x00 &lt;i&gt;",
    "<img src=x onerror=alert(1)>Team",
]
_BAD = re.compile(r"[<>‪-‮⁦-⁩\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|script|onerror|javascript")


def _comicinfo_of(path: Path) -> ET.Element:
    with zipfile.ZipFile(io.BytesIO(path.read_bytes())) as archive:
        return ET.fromstring(archive.read("ComicInfo.xml"))  # noqa: S314 (our own synthetic output)


def _follow_and_export(bench: Bench, title: str, team: str) -> tuple[ET.Element, dict[str, str]]:
    env = export_env_on(bench.lib)
    with bench.lib.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == bench.edition_id)
            .values(shelf_id=env.shelf_id)
        )
    _watermark(bench, "2")
    chapter = new_chapter(5030, 3)
    chapter["name"], chapter["scanlator"] = title, team
    bench.alpha()["chapters"].append(chapter)
    bench.run()
    _settle(bench)
    stored = bench.rows(
        "SELECT u.title AS title, t.label AS team FROM unit_variant v "
        "JOIN unit u ON u.id = v.unit_id LEFT JOIN team t ON t.id = v.team_id "
        "WHERE u.edition_id = :e AND u.number_norm = '3'",
        e=bench.edition_id,
    )[0]
    env.pipeline.run(env.destination_id, job_id=new_ulid())
    exported = [p for p in env.files() if p.name.endswith(".cbz") and "Ch.3" in p.name]
    assert len(exported) == 1, [p.name for p in env.files()]
    return _comicinfo_of(exported[0]), stored


@pytest.mark.req("ENF-SEC-015")
@pytest.mark.parametrize("title", HOSTILE_TITLES)
def test_chapter_title_sanitized_to_comicinfo(title: str) -> None:
    with follow_bench() as bench:
        info, stored = _follow_and_export(bench, title, "Synthetic Team A")
        assert not _BAD.search(stored["title"] or ""), stored
        assert not _BAD.search(info.findtext("Title") or ""), ET.tostring(info)
        assert "chapter" in (info.findtext("Title") or "").lower()


@pytest.mark.req("ENF-SEC-015")
@pytest.mark.parametrize("team", HOSTILE_TEAMS)
def test_team_name_sanitized_to_comicinfo(team: str) -> None:
    with follow_bench() as bench:
        info, stored = _follow_and_export(bench, "Chapter 3", team)
        assert not _BAD.search(stored["team"] or ""), stored
        assert not _BAD.search(info.findtext("Translator") or ""), ET.tostring(info)
        assert "team" in (info.findtext("Translator") or "").lower()


@pytest.mark.req("ENF-TIME-001")
def test_future_upload_date_does_not_keep_a_team_active() -> None:
    far_future = "4070908800000"  # 2099-01-01
    with follow_bench() as bench:
        _watermark(bench, "0")
        held = []
        for n in (1, 2, 3):
            chapter = _chapter(n, "B", 250 - n)
            chapter["uploadDate"] = far_future
            held.append(chapter)
        _serve(bench, held)
        bench.run()
        _settle(bench)  # team B holds three files on disk, "released" in 2099 by its own claim
        last = bench.rows("SELECT last_release_at AS at FROM team WHERE label = 'Team B'")[0]["at"]
        assert str(last).startswith("2026-11"), last  # the server's receipt, not the claim
        bench.clock.advance(days=240)  # B has been silent for 240 days, A just released
        _serve(bench, [*held, _chapter(4, "B", 240), _chapter(4, "A", 1)])
        with structlog.testing.capture_logs() as logs:
            bench.run()
        assert [e["reason"] for e in logs if e["event"] == "team.fallback"] == ["majority_inactive"]
