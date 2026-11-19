"""Second avis adverse de #374 (M5 L17a, `/lite`), indépendant de la première relecture
(docs/decisions/M5.md, « #374 : contradicteur », et tests/integration/test_pr374_contradicteur.py :
rien ici ne refait ce qui y est prouvé — le cookie, sa signature, son sujet, le CSRF, la couverture
cachée, l'oracle du secret, l'échappement des titres, le cache).

Chaque test vise un trou cherché sur la branche et non couvert ailleurs : les bornes de pagination
et de partie, un compte d'Œuvres cachées par le nombre de résultats, un nom de bloc qui tente une
injection d'en-tête, les chemins encodés (%2e%2e, double encodage, Unicode, nom de route encodé), un
en-tête Host ou X-Forwarded-* qui changerait les liens, le cookie présenté à une autre porte, et
l'écho d'une page d'erreur. Un trou réel serait un xfail strict : il y en a **un** — la page 404
d'une requête mal formée n'est pas la page d'une œuvre cachée, contrairement à la promesse
« caché, absent et mal formé : la même page 404, octet pour octet » (décisions M5, L17a).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from tests.integration.test_l48_opds import ABSENT_ULID, FakeBundles, World, make_world
from tests.integration.test_m5_l17a_lite import CSRF_FIELD, Links, browser, signed_in
from tests.visibility_env import HIDDEN_TITLES

pytestmark = pytest.mark.req("EF-LEC-010", "ENF-SEC-010", "EF-USR-006")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with make_world(monkeypatch, settings, FakeBundles()) as made:
        yield made


class _HostileName(FakeBundles):
    """Un fournisseur dont le nom de bloc tente une injection d'en-tête et de HTML : la route doit
    le nettoyer deux fois (safe_filename, puis attachment_disposition) et ne montrer que le reste."""

    def __init__(self) -> None:
        super().__init__()
        self.filename = "pwned\"; filename=\"x.cbz'; charset=utf-8; x=y"


def _session_cookie(client: TestClient) -> tuple[str, str]:
    for cookie in client.cookies.jar:
        if "lite" in cookie.name.lower() and "flow" not in cookie.name.lower():
            return cookie.name, cookie.value or ""
    raise AssertionError("no /lite session cookie")


# -- le trou : « mal formé » est une seconde page ----------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason="la page 404 d'une requête mal formée est bâtie sur une page nue, sans l'en-tête du "
    "lecteur (session.py, la branche RequestValidationError de LiteRoute), pas sur celle d'absent",
)
def test_second_review_a_malformed_entry_is_the_absent_page(world: World) -> None:
    """« Caché, absent et mal formé : la même page 404 (octet pour octet) » (décisions M5, L17a ;
    routes.py, docstring du module). Une entrée hors bornes — offset, partie, requête trop longue —
    doit répondre la page d'une œuvre cachée à un enfant. Elle répond aujourd'hui une page sans
    l'en-tête du lecteur signé : la promesse n'est tenue que pour les identifiants mal formés."""
    kid = signed_in(world, world.kid, "10.0.4.1")
    absent = kid.get(f"/lite/works/{ABSENT_ULID}")
    assert absent.status_code == 404
    assert 'action="/lite/sign-out"' in absent.text  # la page d'un lecteur signé porte son en-tête
    for path, params in (
        ("/lite/works", {"offset": "-1"}),
        ("/lite/works", {"offset": "100001"}),
        ("/lite/works", {"offset": "99999999999999999999"}),
        ("/lite/works", {"offset": "abc"}),
        ("/lite/search", {"q": "x" * 500}),
        (f"/lite/shelves/{world.seed.shelves['adult']}", {"offset": "-1"}),
        (f"/lite/editions/{world.seed.editions['ok']}/complete", {"part": "-1"}),
        (f"/lite/editions/{world.seed.editions['ok']}/complete", {"part": "1000"}),
    ):
        got = kid.get(path, params=params)
        assert (got.status_code, got.content) == (404, absent.content), (path, params)


# -- les sondes qui tiennent ---------------------------------------------------------------------


def test_second_review_out_of_range_entries_are_pages(world: World) -> None:
    """Une pagination ou une partie hors bornes ne sort jamais des pages : la 404 de `/lite`,
    jamais un plantage, jamais une autre route, jamais l'entrée reflétée, et rien au bout."""
    mum = signed_in(world, world.mum, "10.0.4.2")
    entries = (
        ("/lite/works", {"offset": "-1"}),
        ("/lite/works", {"offset": "100001"}),
        ("/lite/works", {"offset": "99999999999999999999"}),
        ("/lite/works", {"offset": "abc"}),
        ("/lite/recent", {"offset": "1e3"}),
        ("/lite/series", {"offset": "-1"}),
        ("/lite/search", {"offset": "-1"}),
        (f"/lite/shelves/{world.seed.shelves['adult']}", {"offset": "-1"}),
        (f"/lite/editions/{world.seed.editions['ok']}/complete", {"part": "0abc"}),
        (f"/lite/editions/{world.seed.editions['ok']}/complete", {"part": "1000"}),
    )
    for path, params in entries:
        got = mum.get(path, params=params)
        assert got.status_code == 404, (path, params)
        assert "text/html" in got.headers["content-type"], (path, params)
        assert got.headers["cache-control"] == "no-store", (path, params)
        assert "cookie" in got.headers.get("vary", "").lower(), (path, params)
        assert next(iter(params.values())) not in got.text, (path, params)
    kid = signed_in(world, world.kid, "10.0.4.3")
    end = kid.get("/lite/works", params={"offset": "100000"})  # en bornes, au-delà du bout
    assert end.status_code == 200
    assert '<ul class="rows">\n</ul>' in end.text
    assert "?offset=99975" in end.text  # le marcheur ne fabrique jamais un lien hors bornes
    world.clean(end.text)


def test_second_review_the_pager_never_announces_hidden_works(world: World) -> None:
    """Un compte d'Œuvres cachées : « Tale » tient les douze Œuvres de l'index, l'enfant n'en voit
    trois, et la page n'annonce jamais davantage ; une requête qui ne trouve que du caché a la
    même forme qu'une requête qui ne trouve rien."""
    kid = signed_in(world, world.kid, "10.0.4.4")
    every = kid.get("/lite/search", params={"q": "Tale"})
    found = {h for h in Links(every.text).hrefs if h.startswith("/lite/works/")}
    assert found == {
        f"/lite/works/{world.seed.works[k]}" for k in ("ok", "adult_shelf", "mixed")
    }
    assert not [h for h in Links(every.text).hrefs if "offset=" in h]
    world.clean(every.text)
    hidden_only = kid.get("/lite/search", params={"q": HIDDEN_TITLES["above"]})
    nothing = kid.get("/lite/search", params={"q": "zzz-no-such-word"})
    assert hidden_only.status_code == nothing.status_code == 200
    assert Links(hidden_only.text).hrefs == Links(nothing.text).hrefs
    assert '<ul class="rows">\n</ul>' in hidden_only.text
    # la requête d'une enfant n'est pas une fuite : le titre caché ne sort que de sa propre boîte
    assert "/lite/works/" not in hidden_only.text
    for offset in (25, 50, 100000):  # marcher au bout d'une recherche vide ne sort rien du caché
        far = kid.get("/lite/search", params={"q": HIDDEN_TITLES["above"], "offset": offset})
        assert far.status_code == 200, offset
        assert "/lite/works/" not in far.text, offset  # ni le titre, ni l'adresse d'une œuvre cachée
        assert not [h for h in Links(far.text).hrefs if h.startswith("/lite/works/")], offset


def test_second_review_encoded_paths_answer_the_absent_page(world: World) -> None:
    """%2e%2e, double encodage, Unicode, « ..;/ » : un chemin encodé ne quitte jamais `/lite` —
    il répond la page d'un absent, octet pour octet, jamais celle d'une autre route."""
    kid = signed_in(world, world.kid, "10.0.4.5")
    absent = kid.get(f"/lite/works/{ABSENT_ULID}")
    shelf, edition, digest = (
        world.seed.shelves["adult"],
        world.seed.editions["ok"],
        world.covers["ok"],
    )
    for path in (
        f"/lite/covers/{digest}/..%2f..%2fx",
        "/lite/covers/..%2f..%2fapi/v1/me/160",
        f"/lite/editions/{edition}/complete/..%2f..%2fapi",
        f"/lite/editions/{edition}/..%2fcomplete",
        "/lite/editions/..%2f..%2fapi/complete",
        f"/lite/shelves/{shelf}/..%2f..%2fapi",
        "/lite/%2e%2e/works",
        "/lite/..%2f..%2fapi/v1/me",
        "/lite/works/..%2f..%2f..%2fapi/v1/me",
        "/lite/..%252f..%252fapi/v1/me",
        "/lite/%252e%252e/works",
        "/lite/works/%ef%bc%8e%ef%bc%8e/x",
        "/lite/works/%c0%af%c0%af/x",
        "/lite/..;/api/v1/me",
        "/lite/.%2e/.%2e/api/v1/me",
        "/lite/sign-in/..%2f..%2flogin",
    ):
        got = kid.get(path)
        assert (got.status_code, got.content) == (404, absent.content), path
        assert "api/v1/me" not in got.text, path


def test_second_review_an_encoded_route_name_reaches_the_same_page(world: World) -> None:
    """Le nom d'une route encodé n'est pas un déguisement : `%77orks` est la page des œuvres, au
    byte près, avec le même filtre ; et un préfixe `/lite` encodé ne vole pas de page — le cookie
    ne suit pas, la réponse est la redirection vers la connexion."""
    kid = signed_in(world, world.kid, "10.0.4.6")
    plain = kid.get("/lite/works")
    decoded = kid.get("/lite/%77orks")
    assert (decoded.status_code, decoded.content) == (plain.status_code, plain.content)
    world.clean(decoded.text)
    other = kid.get("/%6c%69%74%65/works", follow_redirects=False)
    assert other.status_code == 303 and "sign-in" in other.headers["location"]
    assert world.seed.title_of("above") not in other.text


def test_second_review_untrusted_forwarded_headers_change_no_link(world: World) -> None:
    """Host, X-Forwarded-Host, -Prefix, -Proto, -For d'un pair qui n'est pas un proxy de
    confiance : ni les liens, ni la redirection, ni le cookie n'en tiennent compte."""
    hostile = {
        "Host": "elsewhere.example",
        "X-Forwarded-Host": "elsewhere.example",
        "X-Forwarded-Prefix": "/evil",
        "X-Forwarded-Proto": "http",
        "X-Forwarded-For": "6.6.6.6",
    }
    client = browser(world, "10.0.4.7")
    form = client.get("/lite/sign-in", headers=hostile)
    token = CSRF_FIELD.search(form.text)
    assert token
    done = client.post(
        "/lite/sign-in",
        data={"identifier": world.mum.public_id, "secret": world.mum.secret, "csrf": token.group(1)},
        headers={**hostile, "Origin": "https://elsewhere.example"},
        follow_redirects=False,
    )
    assert done.status_code == 303
    assert done.headers["location"] == "/lite"  # relative : aucun hôte ni préfixe annoncé
    session = next(c for c in done.headers.get_list("set-cookie") if c.startswith("__Secure-lite="))
    for wanted in ("Secure", "HttpOnly", "SameSite=strict", "Path=/lite"):
        assert wanted in session  # le schéma annoncé en clair n'a pas déclassé le cookie
    home = client.get("/lite", headers=hostile)
    assert home.status_code == 200
    hrefs = Links(home.text).hrefs
    assert hrefs and all(h.startswith("/lite") for h in hrefs), hrefs
    assert "elsewhere.example" not in home.text and "/evil" not in home.text


def test_second_review_the_lite_cookie_opens_no_other_door(world: World) -> None:
    """Le cookie de `/lite` présenté à la main aux portes qui servent les mêmes données — la
    couverture du front, le profil, le flux OPDS, une liseuse Kobo, les métriques : refus partout,
    aucune page ne sort."""
    kid = signed_in(world, world.kid, "10.0.4.8")
    name, value = _session_cookie(kid)
    other = browser(world, "10.0.4.9")
    digest = world.covers["ok"]
    for path in (
        f"/api/v1/thumbnails/{digest}/160",
        "/api/v1/me",
        "/opds",
        "/kobo/x",
        "/metrics",
    ):
        got = other.get(path, headers={"Cookie": f"{name}={value}"}, follow_redirects=False)
        assert got.status_code in (401, 403, 404), path
        assert world.seed.title_of("ok") not in got.text, path
        assert "/lite/works" not in got.text, path


def test_second_review_a_refused_sign_in_shows_neither_identifier_nor_secret(
    world: World,
) -> None:
    """La page 401 qui rend le formulaire : l'identifiant hostile et le secret saisis ne
    s'y retrouvent pas, ni en clair ni comme balise."""
    client = browser(world, "10.0.4.10")
    form = client.get("/lite/sign-in")
    token = CSRF_FIELD.search(form.text)
    assert token
    hostile = "<script>notes</script>AAAAAAAAAAAAAAAA"
    got = client.post(
        "/lite/sign-in",
        data={"identifier": hostile, "secret": "z" * 26, "csrf": token.group(1)},
        follow_redirects=False,
    )
    assert got.status_code == 401
    assert hostile not in got.text and "z" * 26 not in got.text
    assert "<script" not in got.text.lower()
    assert got.headers["cache-control"] == "no-store"


def test_second_review_a_hostile_block_name_neither_injects_nor_shows_it_raw(
    monkeypatch: pytest.MonkeyPatch, settings: Settings,
) -> None:
    """Le nom du bloc vient du fournisseur : tenté d'injecter, il ne passe ni dans
    Content-Disposition (un seul nom, rien d'autre entre guillemets, aucun retour à la ligne), ni
    dans la page, qui ne montre que le reste nettoyé et échappé."""
    with make_world(monkeypatch, settings, _HostileName()) as made:
        kid = signed_in(made, made.kid, "10.0.4.11")
        page = kid.get(f"/lite/works/{made.seed.works['ok']}")
        assert 'filename="x' not in page.text  # les guillemets du nom sont partis au lavage
        got = kid.get(f"/lite/editions/{made.seed.editions['ok']}/complete")
        assert got.status_code == 200
        disposition = got.headers["content-disposition"]
        assert disposition.startswith('attachment; filename="')
        quoted = disposition.split('filename="', 1)[1].split('"', 1)[0]
        assert quoted and all(c.isalnum() or c in "._- " for c in quoted)
        assert "\r" not in disposition and "\n" not in disposition
        assert disposition.count("filename=") == 1  # un seul nom lisible, le second est codé
        encoded = disposition.split("filename*=UTF-8''", 1)[1]
        assert encoded and not set(encoded) & set("\"'; \r\n")
        assert made.bundles is not None
        assert made.seed.editions["ok"] in made.bundles.offered
        assert made.bundles.opened == [made.seed.editions["ok"]]
