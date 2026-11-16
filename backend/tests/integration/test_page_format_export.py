"""M4 L53 (EF-EXP-046 amended by ADR-0054): what leaves, for a destination or on OPDS, holds pages
in allowlisted formats only. The export renders from the canonical copy, so a library that was
filed before the normalization (or without /store) still gives clean archives."""

from __future__ import annotations

import io
import zipfile
from typing import Any

import pytest

from shelfhold.export.application.page_policy import PagePolicy
from shelfhold.export.application.rendering import CbzRenderer
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.infrastructure.content_trust.cbz_entries import sniff_page_kind
from shelfhold.library.domain.content_hash import content_hash
from tests import page_formats_env as pf
from tests.bundle_env import Chapter, page
from tests.integration.test_l49a_bundle import complete, lab, library, names_of  # noqa: F401
from tests.library_env import LibEnv, lib_env

pytestmark = pytest.mark.skipif(not pf.has_avif(), reason="this Pillow has no AVIF")

INFO = b"<ComicInfo><Series>S</Series></ComicInfo>"


def render(env: LibEnv, data: bytes, policy: PagePolicy | None) -> Any:
    source = env.manga / "S" / "S - 1.cbz"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(data)
    renderer = CbzRenderer(env.fsops, staging_dir=env.tmp, pages=policy)
    return renderer.render(str(source), INFO)


def kinds(rendered: Any) -> dict[str, str | None]:
    with zipfile.ZipFile(rendered.file) as archive:
        rendered.file.seek(0)
        return {
            i.filename: sniff_page_kind(archive.read(i)[:16])
            for i in archive.infolist()
            if i.filename != "ComicInfo.xml"
        }


@pytest.mark.req("EF-EXP-046")
def test_a_destination_receives_pages_in_allowlisted_formats_only() -> None:
    with lib_env() as env:
        rendered = render(env, pf.mixed_cbz(), PagePolicy())
        try:
            assert set(kinds(rendered).values()) <= {"jpeg", "png", "gif", "webp"}
            assert list(kinds(rendered)) == ["001.jpg", "002.png", "003.jpg", "004.webp"]
        finally:
            rendered.file.close()


@pytest.mark.req("EF-EXP-046")
def test_the_content_identity_of_the_unit_is_the_one_of_the_source() -> None:
    data = pf.mixed_cbz()
    with lib_env() as env:
        rendered = render(env, data, PagePolicy())
        try:
            with safe_open_archive(io.BytesIO(data), name="s.cbz") as archive:
                assert rendered.content_hash == content_hash(archive)  # what the plan compares
            assert rendered.pages_normalized is True
        finally:
            rendered.file.close()


@pytest.mark.req("EF-EXP-046")
def test_a_destination_that_takes_only_jpeg_gets_only_jpeg() -> None:
    data = pf.cbz([("1.webp", pf.webp()), ("2.png", pf.png_alpha(10)), ("3.jpg", pf.jpeg())])
    with lib_env() as env:
        rendered = render(env, data, PagePolicy.for_destination({"jpeg"}))
        try:
            assert set(kinds(rendered).values()) == {"jpeg"}
        finally:
            rendered.file.close()


@pytest.mark.req("EF-EXP-046")
def test_an_already_clean_archive_is_rendered_with_its_pages_untouched() -> None:
    data = pf.cbz([("1.jpg", pf.jpeg()), ("2.png", pf.png_alpha(10))])
    with lib_env() as env:
        rendered = render(env, data, PagePolicy())
        try:
            assert rendered.pages_normalized is False
            with zipfile.ZipFile(io.BytesIO(data)) as source:
                rendered.file.seek(0)
                with zipfile.ZipFile(rendered.file) as out:
                    assert out.read("1.jpg") == source.read("1.jpg")
                    assert out.read("2.png") == source.read("2.png")
        finally:
            rendered.file.close()


@pytest.mark.req("EF-EXP-046")
def test_a_page_that_cannot_be_read_leaves_the_archive_as_received() -> None:
    data = pf.cbz([("1.avif", pf.avif()), ("2.avif", pf.truncated_avif())])
    with lib_env() as env:
        rendered = render(env, data, PagePolicy())
        try:
            assert list(kinds(rendered)) == ["1.avif", "2.avif"]  # not half-converted
            assert rendered.pages_normalized is False
            assert [(i.entry, i.code) for i in rendered.page_issues] == [
                ("2.avif", "page.undecodable")
            ]
        finally:
            rendered.file.close()


@pytest.mark.req("EF-EXP-046", "EF-INT-007")
def test_the_opds_block_of_a_normalized_chapter_holds_only_allowlisted_formats(
    lab: Any,  # noqa: F811
) -> None:
    with lib_env() as env:
        rendered = render(env, pf.mixed_cbz(), PagePolicy())
        try:
            rendered.file.seek(0)
            with zipfile.ZipFile(rendered.file) as out:
                pages = [
                    (i.filename, out.read(i))
                    for i in out.infolist()
                    if i.filename != "ComicInfo.xml"
                ]
        finally:
            rendered.file.close()
    lib = library(lab)
    lib.add(Chapter("1", pages))
    lib.add(Chapter("2", [("a.avif", page("avif", "x"))]))  # not normalized: left out, as before
    members = [n.split("/")[1] for n in names_of(complete(lab.mum, lab).content)]
    assert members and {m.rsplit(".", 1)[1] for m in members} <= {"jpg", "png", "gif", "webp"}
    assert len(members) == 4  # the normalized chapter is in whole, the AVIF one is not
