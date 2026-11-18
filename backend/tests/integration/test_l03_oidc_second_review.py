"""M5 L03, second avis sur la livraison OIDC (relecture de la relecture #362).

Le cas « une autre session porte le biscuit de flux » de la relecture #362 n'a jamais été
joué pour de vrai : le biscuit volé y est posé dans le bocal avec `domain=` sur un hôte sans
point, et la tentative part sans rien présenter — sans quoi son propre test ne passerait pas
(la tentative réelle consomme le lien en attente, et sa dernière ligne affirme justement que
la bonne session peut encore confirmer). Ici le biscuit est transplanté tel que le serveur
l'a posé, et la page de confirmation répond au voleur avant l'essai : la présentation est
prouvée, pas supposée.

- ce qui tient : la confirmation d'une autre session est refusée, du même 409 que s'il
  n'y avait rien en attente (aucun indice sur le contrôle qui a échoué), et n'écrit rien ;
- PoC rouge : cette tentative refusée consomme le lien en attente
  (`identity/application/oidc.py`, `confirm_link` : le `take` précède la comparaison des
  sessions) — l'autre personne d'un navigateur partagé peut rompre la liaison en cours et
  forcer le titulaire à tout recommencer, le contraire de ce qu'affirme le test fusionné ;
- la page de confirmation ne peut pas être encadrée : `X-Frame-Options` et `frame-ancestors`
  sont posés par la couche d'entêtes commune à toutes les réponses, ce que la relecture ne
  vérifiait pas (elle ne testait que le CSP).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from shelfhold.config import Settings
from tests.fakes.oidc.provider import ISSUER
from tests.integration.test_m5_l03_oidc import FLOW_COOKIE, LINK, OIDC, World, world_for

pytestmark = pytest.mark.req("EF-USR-004")

CONFIRM = f"{OIDC}/link/confirm"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with world_for(monkeypatch, settings) as w:
        yield w


def _theft_setup(world: World) -> None:
    """The holder's link comes back pending; the flow cookie is transplanted into another
    person's browser exactly as the server set it (not given a domain of its own), and that
    browser seeing the confirmation page is the proof the cookie was presented."""
    started = world.send(world.mum, "POST", LINK)
    assert started.status_code == 200, started.text
    back = world.callback(world.mum, world.idp.authorize(started.json()["authorization_url"]))
    assert back.status_code == 303 and back.headers["location"] == CONFIRM
    flow = world.mum.cookies.get(FLOW_COOKIE)
    assert flow
    stolen = next(c for c in world.mum.cookies.jar if c.name == FLOW_COOKIE)
    world.dad.cookies.jar.set_cookie(stolen)
    page = world.dad.get(CONFIRM)
    assert page.status_code == 200 and "mum" in page.text, (page.status_code, page.text[:200])


def test_oidc_another_sessions_confirmation_is_refused_and_writes_nothing(world: World) -> None:
    """What the first review believed it had covered: another person's session, the browser's
    flow cookie really presented. The refusal is the same one as with nothing pending, so the
    thief learns nothing, and no row is written."""
    _theft_setup(world)
    theft = world.send(world.dad, "POST", CONFIRM, {"confirm": True})
    assert theft.status_code == 409 and theft.json()["code"] == "oidc_link_expired"
    assert world.identities() == []
    assert "oidc.linked" not in world.actions()


@pytest.mark.xfail(strict=True)
def test_oidc_a_refused_theft_leaves_the_holders_link_confirmable(world: World) -> None:
    """A link refused to another session is not the holder's to lose: her confirmation must
    still be hers to give. Today `confirm_link` takes the pending link before it compares the
    sessions, so anyone sharing the browser with a session of their own can burn her link."""
    _theft_setup(world)
    theft = world.send(world.dad, "POST", CONFIRM, {"confirm": True})
    assert theft.status_code == 409 and world.identities() == []
    done = world.send(world.mum, "POST", CONFIRM, {"confirm": True})
    assert done.status_code == 204, done.text
    assert world.identities() == [(world.ids["mum"], ISSUER, world.idp.sub)]
    assert "oidc.linked" in world.actions()


def test_oidc_the_confirmation_page_cannot_be_framed(world: World) -> None:
    """A page that offers to link an identity to the account must not answer inside another
    site's frame: the middleware's `X-Frame-Options` and `frame-ancestors` cover what the
    first review's locked-down test did not look at."""
    started = world.send(world.mum, "POST", LINK)
    assert started.status_code == 200, started.text
    answer = world.callback(world.mum, world.idp.authorize(started.json()["authorization_url"]))
    assert answer.status_code == 303 and answer.headers["location"] == CONFIRM
    page = world.mum.get(CONFIRM)
    assert page.status_code == 200
    assert page.headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
