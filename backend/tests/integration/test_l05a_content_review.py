"""Relectures adverses de L05a : PoC de la borne d'extrait (ADR-0055 D-P1, N3).

La première relecture (#361) a montré que `ContentAccess` prenait la liste d'entrées de l'appelant
et lisait l'archive entière : corrigé, ses deux PoC restent comme tests de régression. La seconde
relecture (2026-11-18, après le correctif) attaque ce qui a remplacé ce choix : `epub_excerpt` rend,
à l'émission comme à chaque service, la liste fermée exacte que `/content` sert pour la portée
`excerpt`. Chaque PoC est un trou réel, rouge tant qu'il n'est pas corrigé (xfail strict ;
`--runxfail` le montre rouge, la marque ôtée quand il est corrigé).
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.archives import safe_open_archive
from shelfhold.library.adapters.epub_excerpt import epub_excerpt
from tests.content_env import add_carrier
from tests.integration.test_l05a_content_route import World, _world

WHOLE = (
    b"<html xmlns='http://www.w3.org/1999/xhtml'><body>"
    + b"".join(b"<h1>chapitre %d</h1><p>%s</p>" % (i, b"x" * 2000) for i in range(1, 21))
    + b"</body></html>"
)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    yield from _world(monkeypatch, settings)


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_poc_excerpt_of_a_single_document_epub_is_never_the_whole_book(world: World) -> None:
    single = add_carrier(world.seed.engine, world.seed.editions["ok"], format="epub")
    world.src.by_content[single] = {"OEBPS/book.xhtml": WHOLE}
    try:
        ticket = world.ticket(carrier=single, scope="excerpt", entries=["OEBPS/book.xhtml"])
    except ValueError:
        return  # refus typé à l'émission : la borne tient
    got = world.get(world.admin, ticket, "OEBPS/book.xhtml")
    assert got.status_code == 404 or len(got.content) < len(WHOLE)


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_poc_excerpt_list_is_computed_by_the_server_not_supplied(world: World) -> None:
    every = sorted(world.src.by_content[world.open_item])  # tout le livre, pages comprises
    with pytest.raises(ValueError):
        world.ticket(scope="excerpt", entries=every)


# -- seconde relecture indépendante (après le correctif de #361) ---------------------------
# La borne de D-P1/N3 doit tenir contre un fichier construit pour la déjouer : l'ordre de lecture
# et les « ressources » viennent du livre, et `epub_excerpt` en fait la liste fermée que
# `/content` sert. Les PoC suivants lisent cette liste sur l'archive réelle qu'elle servira.

_CONTAINER = (
    b'<?xml version="1.0"?><container version="1.0" '
    b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
    b'<rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
    b"</rootfiles></container>"
)
_PAGE = "<html xmlns='http://www.w3.org/1999/xhtml'><head>{head}</head><body>{body}</body></html>"


def _epub(docs: dict[str, str], refs: list[str], extra: dict[str, bytes]) -> bytes:
    """Un EPUB dont l'ordre de lecture (`refs`, des idrefs, doublons permis) et les fichiers hors
    manifeste (`extra`) sont choisis par le test : rien ne vient d'un appelant de la route."""
    items = "".join(
        f'<item id="d{i}" href="{n}" media-type="application/xhtml+xml"/>'
        for i, n in enumerate(docs)
    )
    spine = "".join(f'<itemref idref="{r}"/>' for r in refs)
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        f"<manifest>{items}</manifest><spine>{spine}</spine></package>"
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as archive:
        archive.writestr("META-INF/container.xml", _CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
        for name, text in docs.items():
            archive.writestr(f"OEBPS/{name}", text)
        for name, data in extra.items():
            archive.writestr(name, data)
    return out.getvalue()


def _cut(data: bytes) -> frozenset[str] | None:
    with safe_open_archive(io.BytesIO(data), name="b.epub") as archive:
        return epub_excerpt(archive)


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_poc_a_single_document_repeated_in_the_spine_has_no_excerpt() -> None:
    whole = _PAGE.format(head="", body="<p>" + "x" * 50_000 + "</p>")
    got = _cut(_epub({"book.xhtml": whole}, ["d0"] * 5, {}))
    assert got is None, f"l'extrait d'un livre à un seul document est ce document entier : {got}"


@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_poc_a_document_beyond_the_bound_is_not_a_resource_of_the_excerpt() -> None:
    first = _PAGE.format(head='<link href="rest.xhtml"/>', body="<p>" + "x" * 100 + "</p>")
    second = _PAGE.format(head="", body="<p>" + "x" * 5_000 + "</p>")
    rest = _PAGE.format(head="", body="<p>" + "x" * 40_000 + "</p>").encode()
    got = _cut(
        _epub(
            {"c1.xhtml": first, "c2.xhtml": second},
            ["d0", "d1"],
            {"OEBPS/rest.xhtml": rest},
        )
    )
    assert got is None or "OEBPS/rest.xhtml" not in got, (
        f"la liste fermée sert un document hors de l'ordre de lecture "
        f"(≈ 88 % de la publication) : {got}"
    )
