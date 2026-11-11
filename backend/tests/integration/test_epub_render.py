"""EPUB passthrough through the database: no age in the file, the original OPF kept in /data/meta
and referenced by `unit.json` (EF-EXP-033 / Z3-a, EF-BIB-048)."""

import hashlib
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
    env: ExportEnv,
    tmp_path: Path,
    *,
    age: int | None,
    rights: str | None,
    source_epub: bytes | None = None,
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
    source.write_bytes(source_epub or epub())
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


AGED_ORIGIN = (
    '    <meta property="schema:typicalAgeRange">16+</meta>\n'
    "    <dc:subject>18+</dc:subject>\n"
    '    <meta name="calibre:rating" content="8"/>\n'
    '    <meta property="ibooks:age-rating">17+</meta>\n'
    '    <meta property="schema:typicalAgeRange" id="ag">16+</meta>\n'
    '    <meta refines="#ag" property="role">x</meta>\n'
    '    <opf:meta xmlns:opf="http://www.idpf.org/2007/opf" name="age_rating" content="18"/>\n'
)


@pytest.mark.req("EF-EXP-033")
def test_epub_already_carrying_an_age_leaves_without_one(tmp_path: Path) -> None:
    """Golden: an origin OPF that has an age in every known form leaves with none of them, the
    XHTML head and every other entry are untouched, and the original OPF still has them (D1)."""
    xhtml_head = (
        b'<html xmlns="http://www.w3.org/1999/xhtml"><head><meta name="age" content="18+"/>'
        b"<title>t</title></head><body><p>x</p></body></html>\n"
    )
    source = epub(opf(extra_metadata=AGED_ORIGIN))
    with zipfile.ZipFile(io.BytesIO(source)) as z:
        entries = {n: z.read(n) for n in z.namelist()}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_STORED if name == "mimetype" else zipfile.ZIP_DEFLATED
            z.writestr(info, xhtml_head if name == "OEBPS/c1.xhtml" else data)
            if name == "OEBPS/c1.xhtml":
                entries[name] = xhtml_head
    with export_env() as env:
        _, rendered = render(env, tmp_path, age=16, rights=None, source_epub=buffer.getvalue())
        with rendered.file, zipfile.ZipFile(rendered.file) as archive:
            written = archive.read("OEBPS/content.opf").decode()
            for name in archive.namelist():
                if name != "OEBPS/content.opf":  # outside the OPF: same bytes, so same sha256
                    assert (
                        hashlib.sha256(archive.read(name)).hexdigest()
                        == hashlib.sha256(entries[name]).hexdigest()
                    ), name
        assert b'content="18+"' in xhtml_head and rendered.original_opf.decode().count("16+") == 2
    low = written.lower()
    for needle in (
        "16+",
        "17+",
        "18+",
        "agerange",
        "age-rating",
        "age_rating",
        "rating",
        'id="ag"',
    ):
        assert needle not in low, needle
    assert "<dc:subject>Fiction</dc:subject>" in written
    assert 'page-progression-direction="rtl"' in written
    assert "schema:accessibilitySummary" in written
