"""EPUB passthrough through the database: no age in the file, the original OPF kept in /data/meta
and referenced by `unit.json` (EF-EXP-033 / Z3-a, EF-BIB-048)."""

import io
import json
import os
import re
import zipfile
from pathlib import Path

import pytest
import sqlalchemy as sa

from shelfhold.export.application.rendering import EpubRenderer, RenderedEpub, epub_opf_edit
from shelfhold.infrastructure.schema import catalog
from shelfhold.library.adapters.meta_projection import MetaProjection, meta_path
from shelfhold.library.adapters.origin_opf import keep_origin_opf, origin_opf_rel
from tests.epub_env import epub, opf
from tests.export_env import ExportEnv, export_env
from tests.integration.test_schema_catalog import add_unit

GOLDEN = Path(__file__).parents[1] / "golden" / "epub"


def render(
    env: ExportEnv, tmp_path: Path, *, age: int | None, rights: str | None
) -> tuple[str, RenderedEpub]:
    book = env.new_series("Série synthétique")
    with env.engine.begin() as connection:
        connection.execute(
            sa.update(catalog.work).where(catalog.work.c.id == book.work_id).values(age_rating=age)
        )
        connection.execute(
            sa.update(catalog.edition)
            .where(catalog.edition.c.id == book.edition_id)
            .values(rights=rights)
        )
        unit = add_unit(connection, book.edition_id, "3")
    source = tmp_path / "derivative.epub"
    source.write_bytes(epub())
    with env.engine.connect() as connection:
        edit = epub_opf_edit(connection, unit, "Série synthétique")
    return unit, EpubRenderer(staging_dir=tmp_path).render(str(source), edit)


@pytest.mark.req("EF-EXP-033")
def test_epub_export_writes_no_age_subject_nor_tool_meta(tmp_path: Path) -> None:
    """Golden: an EPUB of a 16+ Work leaves with no age anywhere in its OPF (D1, Z3-a)."""
    with export_env() as env:
        _, rendered = render(env, tmp_path, age=16, rights="CC BY 4.0 https://example.org/b")
        with rendered.file, zipfile.ZipFile(rendered.file) as archive:
            written = archive.read("OEBPS/content.opf")
            everything = b"".join(archive.read(n) for n in archive.namelist())
    text = written.decode().lower()
    assert "shelfhold" not in everything.decode("latin-1").lower()
    for needle in ("16+", "mature", "agerating", "age_rating", "shelfhold:age", ">16<"):
        assert needle not in text
    assert re.search(r"\bage\b", text) is None
    before = [x for x in opf().decode().splitlines() if "dc:subject" in x]
    after = [x for x in written.decode().splitlines() if "dc:subject" in x]
    assert after == before  # still the book's own subject, no age added
    golden = GOLDEN / "opf_16plus.opf"
    if os.environ.get("SHELFHOLD_UPDATE_GOLDEN") == "1":
        golden.write_bytes(written)
    assert golden.read_bytes() == written


@pytest.mark.req("EF-BIB-048", "EF-EXP-033")
def test_original_opf_kept_in_meta_for_extraction(tmp_path: Path) -> None:
    with export_env() as env:
        unit, rendered = render(env, tmp_path, age=16, rights=None)
        rendered.file.close()
        data = env.lib.data
        keep_origin_opf(data, unit, rendered.original_opf)
        assert (data / origin_opf_rel(unit)).read_bytes() == opf()
        keep_origin_opf(data, unit, b"<package/>")  # the first one is the original: kept
        assert (data / origin_opf_rel(unit)).read_bytes() == opf()
        MetaProjection(env.engine, data).project(unit)
        fields = json.loads(meta_path(data, unit).read_bytes())
    reference = fields["origin_opf"]
    assert reference["path"] == origin_opf_rel(unit)
    assert reference["original_sha256"].startswith("sha256:")


@pytest.mark.req("EF-EXP-033")
def test_rendered_epub_is_a_stream_for_the_caller(tmp_path: Path) -> None:
    with export_env() as env:
        _, rendered = render(env, tmp_path, age=None, rights=None)
        with rendered.file:
            raw = rendered.file.read()
    assert len(raw) == rendered.size
    assert zipfile.ZipFile(io.BytesIO(raw)).testzip() is None
