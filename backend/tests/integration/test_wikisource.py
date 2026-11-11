"""M2 L24 (EF-SRC-011, EF-BIB-033, EF-BIB-051): Wikisource, one EPUB per book, with the CC BY-SA
attribution that follows the book into the exported file and the metadata. Each "never" of the lot
is a test that tries to break it; nothing here reaches a network."""

from __future__ import annotations

import asyncio
import io
import re
import zipfile
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from shelfhold.export.application.rendering import EpubRenderer, epub_opf_edit
from shelfhold.infrastructure.fetcher import DomainNotAllowed
from shelfhold.library.public import servable_epub
from shelfhold.plugin_host.errors import NoSourceForUrl
from shelfhold.sdk.testing.corpus.trapped_epubs.build import epub_bytes, opf, xhtml
from tests.fakes.wikisource import Page
from tests.gutenberg_env import CLEAN, TRAPPED, RoutedStore
from tests.wikisource_env import (
    API_HOST,
    BOOK,
    EXPORT_HOST,
    KEY,
    PAGE_URL,
    Bench,
    host_of,
    wikisource_bench,
)

OPF_NS = "{http://www.idpf.org/2007/opf}"
DC = "{http://purl.org/dc/elements/1.1/}"


def with_metadata(extra: str) -> bytes:
    """A clean EPUB whose OPF carries `extra` in its metadata."""
    package = opf().replace(b"</metadata>", extra.encode() + b"</metadata>")
    return epub_bytes(xhtml("<p>chapitre</p>"), opf_bytes=package)


def exported_opf(b: Bench, unit_id: str, tmp: Path) -> Any:
    """The OPF of what the destination receives for `unit_id`: the file as the library serves it,
    the OPF edited by the export (L08)."""
    from shelfhold.infrastructure.xml import safe_parse_xml

    served = servable_epub(b.lib.engine, b.lib.fsops, unit_id)
    with b.lib.engine.connect() as connection:
        edit = epub_opf_edit(connection, unit_id, "Série synthétique")
    rendered = EpubRenderer(staging_dir=tmp).render(str(served.path), edit)
    with zipfile.ZipFile(rendered.file) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".opf"))
        return safe_parse_xml(archive.read(name))


def texts(root: Any, tag: str) -> list[str]:
    return [(e.text or "") for e in root.iter(f"{DC}{tag}")]


# -- one EPUB per book ---------------------------------------------------------------------------


@pytest.mark.req("EF-SRC-011")
def test_wikisource_epub_export() -> None:
    """A search finds the book, the export tool gives its EPUB, and the EPUB is stored; nothing
    but the API and the export tool is asked."""
    with wikisource_bench(exports={BOOK: CLEAN}) as b:
        assert b.search("comte") == ["fr:Le_Comte_de_Test"]
        _prepared, outcome = b.acquire()
        assert outcome.state == "fetched" and outcome.kind == "epub"
        assert {host_of(r) for r in b.wire.recorder.requests} <= {API_HOST, EXPORT_HOST}
        (export,) = [r for r in b.wire.to(EXPORT_HOST) if r.url.path != "/robots.txt"]
        assert dict(export.url.params) == {
            "format": "epub-3",
            "lang": "fr",
            "page": "Le_Comte_de_Test",
        }
        assert export.headers["user-agent"].startswith("Shelfhold/")  # identifiable (L06)
        assert not b.wire.outside
        (work,) = b.rows("SELECT title FROM work")
        assert work["title"] == BOOK


@pytest.mark.req("EF-SRC-011", "EF-SRC-004")
def test_a_wikisource_book_url_gives_the_book() -> None:
    with wikisource_bench() as b:
        ref = b.run(b.acquirer.resolve("https://fr.wikisource.org/wiki/Le_Comte_de_Test"))
        assert ref.source_key == KEY and ref.remote_id == "fr:Le_Comte_de_Test"
        spaced = b.run(b.acquirer.resolve("https://fr.wikisource.org/wiki/Le%20Comte%20de_Test"))
        assert spaced.remote_id == "fr:Le_Comte_de_Test"
        assert not b.wire.recorder.requests  # nothing is asked to name a book


@pytest.mark.req("EF-SRC-011", "EF-SRC-004", "ENF-LEG-003")
@pytest.mark.parametrize(
    "url",
    [
        "https://fr.wikipedia.org/wiki/Le_Comte_de_Test",
        "https://fr.wikisource.org.evil.example/wiki/Le_Comte_de_Test",
        "https://evil.example/fr.wikisource.org/wiki/Le_Comte_de_Test",
        "https://user:pw@fr.wikisource.org/wiki/Le_Comte_de_Test",
        "https://fr.wikisource.org:8443/wiki/Le_Comte_de_Test",
        "https://fr.wikisource.org/w/index.php?title=Le_Comte_de_Test",
        "https://fr.wikisource.org/",
    ],
)
def test_a_url_that_is_not_a_wikisource_book_is_no_source_and_sends_nothing(url: str) -> None:
    with wikisource_bench() as b:
        with pytest.raises(NoSourceForUrl):
            b.run(b.acquirer.resolve(url))
        assert not b.wire.recorder.requests


# -- the attribution: authors, licence, link, in the file and in the metadata --------------------


@pytest.mark.req("EF-BIB-033", "EF-SRC-011")
def test_wikisource_rights_cc_by_sa_exported(tmp_path: Path) -> None:
    """Attack: the exported EPUB carries no attribution of its own. What the destination gets still
    names the contributors, the licence and the link, as `dc:rights` and `dc:source`."""
    with wikisource_bench(exports={BOOK: CLEAN}) as b:
        prepared, outcome = b.acquire()
        assert outcome.state == "fetched"
        root = exported_opf(b, prepared.unit_id, tmp_path)
        rights = " ".join(texts(root, "rights"))
        assert "CC BY-SA" in rights
        assert "Alice Synth" in rights and "Bruno Synth" in rights
        assert texts(root, "source") == [PAGE_URL]
        assert "creativecommons.org/licenses/by-sa" in rights


@pytest.mark.req("EF-BIB-033")
def test_wikisource_attribution_is_on_the_edition_and_in_the_provenance() -> None:
    with wikisource_bench(exports={BOOK: CLEAN}) as b:
        b.acquire()
        (edition,) = b.rows("SELECT id, rights, rights_basis, rights_jurisdiction FROM edition")
        assert edition["rights_basis"] == "licence" and edition["rights_jurisdiction"] is None
        for needle in ("CC BY-SA", "Alice Synth", "Bruno Synth", PAGE_URL):
            assert needle in edition["rights"]
        (field,) = b.rows(
            "SELECT provider, provenance, value FROM metadata_field "
            "WHERE entity_type = 'edition' AND field = 'rights'"
        )
        assert field["provider"] == KEY and field["provenance"] == "source"
        assert "CC BY-SA" in field["value"]


@pytest.mark.req("EF-BIB-033")
def test_an_epub_whose_own_rights_omit_the_licence_still_carries_the_attribution(
    tmp_path: Path,
) -> None:
    """Attack: the export says `All rights reserved` in its own `dc:rights`. The book's own line
    stays (nothing is rewritten) and the attribution is written beside it, so it cannot be lost."""
    book = with_metadata("<dc:rights>Tous droits réservés</dc:rights>")
    with wikisource_bench(exports={BOOK: book}) as b:
        prepared, _ = b.acquire()
        root = exported_opf(b, prepared.unit_id, tmp_path)
        rights = texts(root, "rights")
        assert "Tous droits réservés" in rights
        assert any("CC BY-SA" in r and "Alice Synth" in r for r in rights)


@pytest.mark.req("EF-BIB-033")
def test_an_epub_that_already_carries_the_licence_is_not_given_it_twice(tmp_path: Path) -> None:
    book = with_metadata("<dc:rights>Licence CC-BY-SA 4.0, texte de Wikisource</dc:rights>")
    with wikisource_bench(exports={BOOK: book}) as b:
        prepared, _ = b.acquire()
        root = exported_opf(b, prepared.unit_id, tmp_path)
        assert len(texts(root, "rights")) == 1


@pytest.mark.req("EF-BIB-033", "ENF-SEC-015")
def test_a_contributor_name_cannot_forge_the_markup_nor_the_link_of_the_attribution(
    tmp_path: Path,
) -> None:
    """Attack: a user name with markup, a link, a line break and a huge length. The attribution is
    plain text, the origin link stays the page, and the text is bounded."""
    hostile = (
        "<b>Mallory</b> https://evil.example/pwn",
        "Eve\nhttps://evil.example/x",
        "M" * 5000,
        "Alice Synth",
    )
    pages = {BOOK: Page(BOOK, contributors=hostile)}
    with wikisource_bench(exports={BOOK: CLEAN}, pages=pages) as b:
        prepared, _ = b.acquire()
        (edition,) = b.rows("SELECT rights FROM edition")
        assert "<" not in edition["rights"] and "evil.example" not in edition["rights"]
        assert len(edition["rights"]) <= 2000 and "Alice Synth" in edition["rights"]
        root = exported_opf(b, prepared.unit_id, tmp_path)
        assert texts(root, "source") == [PAGE_URL]


@pytest.mark.req("EF-BIB-033")
def test_a_long_list_of_contributors_is_cut_and_says_so() -> None:
    names = tuple(f"Contributor{i:03d}" for i in range(400))
    pages = {BOOK: Page(BOOK, contributors=names, anonymous=5, more=True)}
    with wikisource_bench(exports={BOOK: CLEAN}, pages=pages) as b:
        b.acquire()
        (edition,) = b.rows("SELECT rights FROM edition")
        assert "Contributor000" in edition["rights"] and "Contributor399" not in edition["rights"]
        assert re.search(r"(other|autre)s?", edition["rights"], re.IGNORECASE)
        assert len(edition["rights"]) <= 2000


@pytest.mark.req("EF-BIB-033")
def test_the_attribution_of_a_new_acquisition_never_rewrites_the_one_of_the_edition() -> None:
    """A second acquisition of the same book keeps the attribution first written."""
    with wikisource_bench(exports={BOOK: CLEAN}) as b:
        b.acquire()
        (before,) = b.rows("SELECT rights FROM edition")
        b.wire.pages[BOOK] = Page(BOOK, contributors=("Zed Synth",))
        b.acquire()
        (after,) = b.rows("SELECT rights FROM edition")
        assert after == before


# -- the domains: the API, the export tool, nothing else ------------------------------------------


@pytest.mark.req("EF-SRC-011", "EF-SRC-003", "ENF-LEG-003")
@pytest.mark.parametrize(
    "url",
    [
        "https://fr.wikisource.org/wiki/Le_Comte_de_Test",  # a page of the wiki: never read
        "https://fr.wikisource.org/",
        "https://fr.wikisource.org/w/../wiki/Le_Comte_de_Test",
        "https://fr.wikisource.org/w/%2e%2e/wiki/X",
        "https://en.wikipedia.org/w/api.php?action=query",
        "https://fr.wikisource.evil.example/w/api.php",
        "https://commons.wikimedia.org/w/api.php",
    ],
)
def test_a_request_outside_the_domain_rules_is_refused_before_any_packet(url: str) -> None:
    with wikisource_bench() as b:
        fetcher = b.fetchers.for_record(b.registry.get(KEY), b.settings)
        with pytest.raises(DomainNotAllowed):
            b.run(fetcher.get(url))
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-011", "EF-SRC-003")
def test_an_export_redirected_off_the_domain_rules_is_refused_at_the_hop() -> None:
    """Attack: the export tool answers with a redirection to another host."""
    with wikisource_bench() as b:
        original = b.wire.respond

        def bounce(request: Any) -> Any:
            import httpx2

            if host_of(request) == EXPORT_HOST and request.url.path == "/":
                return httpx2.Response(302, headers={"location": "https://en.wikipedia.org/x.epub"})
            return original(request)

        b.wire.respond = bounce  # type: ignore[method-assign]
        _prepared, outcome = b.acquire()
        assert outcome.kind != "epub" and not b.wire.outside


@pytest.mark.req("EF-SRC-011", "ENF-SEC-014")
@pytest.mark.parametrize(
    "language",
    ["fr.evil.example", "fr/../x", "fr#", "FR", "f", "fr\n", "a" * 40, "fr.wikisource.org/x?"],
)
def test_a_language_cannot_choose_the_host(language: str) -> None:
    """Attack: the language names the sub-domain; a value that is not a language code is refused
    before a request is built."""
    with wikisource_bench() as b:
        with pytest.raises(Exception) as caught:
            b.search("comte", language=language)
        assert not isinstance(caught.value, AssertionError)
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-011", "ENF-SEC-014")
@pytest.mark.parametrize(
    "remote_id",
    ["evil.example:Page", "fr:", "fr:A#B", "fr:A|B", "fr:A\nB", "fr:" + "x" * 300, ":Page", "Page"],
)
def test_a_reference_that_is_not_a_page_of_a_wikisource_is_refused_without_a_request(
    remote_id: str,
) -> None:
    from shelfhold.sdk.refs import SeriesRef

    with wikisource_bench() as b:
        ref = SeriesRef(source_key=KEY, remote_id=remote_id, title="")

        with pytest.raises(Exception):  # noqa: B017 (a refusal, whatever its type)
            b.run(b.acquirer.prepare(ref))
        assert not b.wire.recorder.requests


@pytest.mark.req("EF-SRC-011")
def test_a_page_outside_the_main_namespace_or_missing_is_never_exported() -> None:
    with wikisource_bench(exports={"Utilisateur:Alice": CLEAN}) as b:
        for title in ("Utilisateur:Alice", "Page Absente"):
            with pytest.raises(Exception):  # noqa: B017 (refused at the book's description)
                b.acquire(title)
        assert not b.wire.to(EXPORT_HOST)


@pytest.mark.req("EF-SRC-011")
def test_an_export_that_is_not_an_epub_is_not_filed_as_one() -> None:
    page = b"<html><body>Error 500</body></html>"
    with wikisource_bench(exports={BOOK: page}) as b:
        _prepared, outcome = b.acquire()
        assert outcome.kind != "epub"
        assert not b.lib.files_under(b.lib.manga)


# -- the untrusted chain, always ----------------------------------------------------------------


@pytest.mark.req("EF-BIB-051", "EF-SRC-011")
def test_a_wikisource_epub_with_active_content_is_neutralized_and_its_original_kept() -> None:
    """Attack: the exported EPUB carries a script (a page can hold anything). The binding is
    `untrusted`, only the neutralized derivative is served, the original stays in /store."""
    with wikisource_bench(exports={BOOK: TRAPPED}) as b:
        assert b.plugin.manifest.content_trust == "untrusted"
        prepared, outcome = b.acquire()
        assert outcome.state == "fetched"
        (binding,) = b.rows("SELECT content_trust FROM source_binding")
        assert binding["content_trust"] == "untrusted"
        (library,) = b.lib.files_under(b.lib.manga)
        with zipfile.ZipFile(io.BytesIO(library.read_bytes())) as archive:
            assert not any(b"<script" in archive.read(n) for n in archive.namelist())
        assert b.lib.files_under(b.lib.store)
        assert Path(servable_epub(b.lib.engine, b.lib.fsops, prepared.unit_id).path) == library


@pytest.mark.req("EF-BIB-051", "EF-SRC-011")
def test_the_trust_value_is_not_a_setting() -> None:
    """Attack: a setting claims the source is `publisher`; the binding still says `untrusted`."""
    with wikisource_bench(exports={BOOK: TRAPPED}, settings={"content_trust": "publisher"}) as b:
        b.acquire()
        (binding,) = b.rows("SELECT content_trust FROM source_binding")
        assert binding["content_trust"] == "untrusted"


# -- the rhythm -----------------------------------------------------------------------------------


@pytest.mark.req("ENF-LEG-003", "EF-SRC-011")
def test_wikisource_rate_limit_is_prudent_and_holds_across_operations() -> None:
    with wikisource_bench(exports={BOOK: CLEAN, "La Tulipe Noire": CLEAN}) as b:
        limit = b.plugin.manifest.rate_limit
        assert limit.requests_per_second <= 1.0 and limit.concurrency == 1
        b.search("comte")
        b.acquire()
        b.acquire("La Tulipe Noire")
        gaps = [later - earlier for earlier, later in pairwise(b.wire.stamps)]
        assert len(b.wire.stamps) >= 6 and min(gaps) >= 1 / limit.requests_per_second - 1e-6


@pytest.mark.req("ENF-LEG-003", "EF-SRC-011")
def test_the_rhythm_holds_for_two_books_asked_at_the_same_time() -> None:
    """Attack: two acquisitions together, each with a Fetcher of its own, share one rhythm over the
    API and the export tool alike."""
    books = {BOOK: CLEAN, "La Tulipe Noire": CLEAN}
    with wikisource_bench(exports=books) as b:

        async def together() -> None:
            prepared = await asyncio.gather(*(b.acquirer.prepare(b.ref(t)) for t in books))
            assert all(p.request_id for p in prepared)
            await asyncio.gather(
                *(
                    b.acquirer.fetch(str(p.request_id), RoutedStore(b.lib), b.place)
                    for p in prepared
                )
            )

        b.run(together())
        gaps = [later - earlier for earlier, later in pairwise(b.wire.stamps)]
        limit = b.plugin.manifest.rate_limit
        assert len(b.wire.stamps) >= 6 and min(gaps) >= 1 / limit.requests_per_second - 1e-6
