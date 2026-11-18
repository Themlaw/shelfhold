"""PR #361, relecture adverse : la borne d'extrait (ADR-0055 D-P1, N3).

`ContentAccess` reçoit sa liste d'entrées de l'appelant et lit toujours l'archive entière : rien
ne coupe un extrait côté serveur. Chaque test est un trou réel, rouge tant qu'il n'est pas corrigé.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
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


@pytest.mark.xfail(
    strict=True,
    reason="#361 C1 : un ticket d'extrait sur un EPUB à un seul document sert le livre entier "
    "(ADR-0055 N3 : dérivé d'extrait coupé côté serveur, ou refus « extrait indisponible »)",
)
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


@pytest.mark.xfail(
    strict=True,
    reason="#361 C1 : la liste fermée d'un extrait est celle que fournit l'appelant, pas celle "
    "que le serveur calcule à l'émission depuis le dérivé (ADR-0055 « Liste fermée »)",
)
@pytest.mark.req("EF-LEC-005", "ENF-SEC-010")
def test_poc_excerpt_list_is_computed_by_the_server_not_supplied(world: World) -> None:
    every = sorted(world.src.by_content[world.open_item])  # tout le livre, pages comprises
    with pytest.raises(ValueError):
        world.ticket(scope="excerpt", entries=every)
