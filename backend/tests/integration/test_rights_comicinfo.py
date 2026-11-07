"""EF-BIB-033: the rights of an Edition are copied into the ComicInfo of its files when they are
absent from it (`Notes` for the text, `Web` for the origin URL), and into nothing else."""

import zipfile

import pytest
import sqlalchemy as sa

from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import catalog
from tests.export_env import ExportEnv, Series, export_env

RIGHTS = "CC BY-SA 4.0 — https://fr.wikisource.org/wiki/Sample (BnF)"


def set_rights(env: ExportEnv, series: Series, rights: str | None) -> None:
    with env.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == series.edition_id)
            .values(rights=rights)
        )


def comicinfo_of(env: ExportEnv) -> str:
    (path,) = [p for p in env.files() if p.suffix == ".cbz"]
    with zipfile.ZipFile(path) as archive:
        return archive.read("ComicInfo.xml").decode("utf-8")


@pytest.mark.req("EF-BIB-033")
def test_rights_copied_to_comicinfo_when_absent() -> None:
    with export_env() as env:
        book = env.new_series("Sample")
        set_rights(env, book, RIGHTS)
        env.add_chapter(book, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        xml = comicinfo_of(env)
        assert "<Notes>CC BY-SA 4.0 — https://fr.wikisource.org/wiki/Sample (BnF)</Notes>" in xml
        assert "<Web>https://fr.wikisource.org/wiki/Sample</Web>" in xml
        assert xml.index("<Notes>") < xml.index("<Web>")
        assert "shelfhold" not in xml.lower()  # the rights are the source's, not the tool's


@pytest.mark.req("EF-BIB-033")
def test_no_rights_no_notes_no_web() -> None:
    with export_env() as env:
        book = env.new_series("Sans droits")
        env.add_chapter(book, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        xml = comicinfo_of(env)
        assert "<Notes>" not in xml and "<Web>" not in xml


@pytest.mark.req("EF-BIB-033")
def test_rights_without_url_leave_web_empty_and_markup_out() -> None:
    with export_env() as env:
        book = env.new_series("Texte")
        set_rights(env, book, "Domaine public <b>mondial</b>")
        env.add_chapter(book, "1")
        env.pipeline.run(env.destination_id, job_id=new_ulid())
        xml = comicinfo_of(env)
        assert "<Web>" not in xml
        assert "<Notes>Domaine public mondial</Notes>" in xml
