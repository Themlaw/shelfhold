"""An adopted series keeps its name to the character (EF-EXP-052, GAP-5-03, E3)."""

import os
import zipfile
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.schema import export
from tests.adopt_env import adoption, comicinfo
from tests.export_env import export_env
from tests.kavita_env import run

GOLDEN = Path(__file__).parent / "adopted_series"
TYPOGRAPHIC = "L’Atelier"  # the apostrophe Kavita and the files carry


@pytest.mark.req("EF-EXP-052", "ENF-QUAL-004")
def test_adopted_series_new_chapter_no_second_kavita_series() -> None:
    with export_env() as env:
        a = adoption(env)
        kavita_id = a.add_series(
            TYPOGRAPHIC,
            [f"{TYPOGRAPHIC} - Ch.{n}.cbz" for n in (1, 2)],
            info=comicinfo(TYPOGRAPHIC, localized="Atelier", sort="Atelier, L"),
        )
        series = env.new_series("L'Atelier")  # the source writes a straight apostrophe
        env.add_chapter(series, "51")
        run(a.adopter.adopt(a.wired.client, env.destination_id, confirmed=True))
        with env.engine.connect() as connection:
            before = connection.execute(sa.select(export.export_series)).mappings().all()
        assert [(r["kavita_series_id"], r["frozen_series"]) for r in before] == [
            (str(kavita_id), TYPOGRAPHIC)
        ]
        assert before[0]["frozen_language"] == "fr" and before[0]["series_dir"] == TYPOGRAPHIC
        env.pipeline.run(env.destination_id, job_id="01J00000000000000000000000")
        with env.engine.connect() as connection:
            after = connection.execute(sa.select(export.export_series)).mappings().all()
            link = connection.execute(sa.select(export.edition_export_series)).mappings().all()
        assert [r["id"] for r in after] == [r["id"] for r in before]  # no second series
        assert len(link) == 1 and link[0]["export_series_id"] == before[0]["id"]
        written = env.root / TYPOGRAPHIC / f"{TYPOGRAPHIC} - Ch.51.cbz"
        assert written.exists() and len(list((env.root / TYPOGRAPHIC).iterdir())) == 3
        with zipfile.ZipFile(written) as zf:
            xml = zf.read("ComicInfo.xml").decode("utf-8")
        assert f"<Series>{TYPOGRAPHIC}</Series>" in xml  # never the source's "L'Atelier"
        assert "L'Atelier" not in xml
        assert env.scans == [f"/manga/Kavita/{TYPOGRAPHIC}"]  # the existing series folder
        target = GOLDEN / "ch51_comicinfo.xml"
        if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
            target.write_text(xml, "utf-8")
        assert target.read_text("utf-8") == xml
