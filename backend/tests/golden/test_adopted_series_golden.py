"""An adopted series keeps its name and its folder to the character (EF-EXP-052, GAP-5-03, E3)."""

import os
import zipfile
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import export
from tests.adopt_env import Adoption, adoption, comicinfo
from tests.export_env import ExportEnv, export_env
from tests.kavita_export_env import LIBRARY_ID, REMOTE_ROOT

GOLDEN = Path(__file__).parent / "adopted_series"
TYPOGRAPHIC = f"L{chr(0x2019)}Atelier"  # the apostrophe Kavita and the files carry


def _export_and_scan(env: ExportEnv, a: Adoption) -> list[str]:
    """Export the new chapter, then let the simulated Kavita scan what was written."""
    env.pipeline.run(env.destination_id, job_id="01J00000000000000000000000")
    new = [r.rel_path for r in env.rows(export.export_record) if r.state != "adopted"]
    for rel in new:
        folder, _, name = f"{REMOTE_ROOT}/{rel}".rpartition("/")
        a.wired.fake.discover(folder, [f"{folder}/{name}"], library_id=LIBRARY_ID)
    return new


def _check_golden(name: str, xml: str) -> None:
    target = GOLDEN / name
    if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
        target.write_text(xml, "utf-8")
    assert target.read_text("utf-8") == xml


@pytest.mark.req("EF-EXP-052", "ENF-QUAL-004")
@pytest.mark.parametrize(
    ("folder", "kavita_name", "golden"),
    [
        (TYPOGRAPHIC, TYPOGRAPHIC, "ch51_comicinfo.xml"),
        ("Atelier (2019)", TYPOGRAPHIC, "ch51_other_folder_comicinfo.xml"),  # folder != name
        (TYPOGRAPHIC, "Atelier of Witch Hat", "ch51_other_kavita_name_comicinfo.xml"),
    ],
    ids=["same", "folder_differs", "kavita_name_differs"],
)
def test_adopted_series_new_chapter_no_second_kavita_series(
    folder: str, kavita_name: str, golden: str
) -> None:
    with export_env() as env:
        a = adoption(env)
        kavita_id = a.add_series(
            folder,
            [f"{TYPOGRAPHIC} - Ch.{n}.cbz" for n in (1, 2)],
            name=kavita_name,
            info=comicinfo(TYPOGRAPHIC, localized="Atelier", sort="Atelier, L"),
        )
        series = env.new_series("L'Atelier")  # the source writes a straight apostrophe
        env.add_chapter(series, "51")
        a.adopt()
        with env.engine.connect() as connection:
            before = connection.execute(sa.select(export.export_series)).mappings().all()
        assert [(r["kavita_series_id"], r["frozen_series"], r["series_dir"]) for r in before] == [
            (str(kavita_id), TYPOGRAPHIC, folder)
        ]
        assert before[0]["name_raw"] == kavita_name and before[0]["frozen_language"] == "fr"
        new = _export_and_scan(env, a)
        assert new == [f"{folder}/{TYPOGRAPHIC} - Ch.51.cbz"]  # in the adopted folder
        with env.engine.connect() as connection:
            after = connection.execute(sa.select(export.export_series)).mappings().all()
            link = connection.execute(sa.select(export.edition_export_series)).mappings().all()
        assert [r["id"] for r in after] == [r["id"] for r in before]  # no second series
        assert len(link) == 1 and link[0]["export_series_id"] == before[0]["id"]
        assert len(a.wired.fake.series) == 1  # Kavita itself, after the scan: still one series
        assert len(list((env.root / folder).iterdir())) == 3
        with zipfile.ZipFile(env.root / new[0]) as zf:
            xml = zf.read("ComicInfo.xml").decode("utf-8")
        assert f"<Series>{TYPOGRAPHIC}</Series>" in xml  # the files' own, never "L'Atelier"
        assert "L'Atelier" not in xml
        assert "<SeriesSort>Atelier, L</SeriesSort>" in xml  # the frozen values, not the Work's
        assert "<LocalizedSeries>Atelier</LocalizedSeries>" in xml
        assert env.scans == [f"{REMOTE_ROOT}/{folder}"]  # the existing series folder
        _check_golden(golden, xml)
