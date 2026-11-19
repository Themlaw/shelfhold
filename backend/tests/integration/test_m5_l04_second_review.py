"""Second avis adverse de M5 L04 : la fraîcheur OIDC des actions réservées (EF-USR-024).

Relecture indépendante, après la réponse au contradicteur de L04 (docs/decisions/M5.md) et
docs/admin/oidc.md : ce qui y est prouvé n'est pas rejoué. La règle « preuve de la session »
tient pour les trois effets qu'elle garde (`ENROLS_PASSKEY`, `MANAGES_CREDENTIALS`,
`LINKS_IDENTITY`, `domain/reserved.py`) : une session fraîche prouvée par le fournisseur seul
n'enregistre pas de clé d'accès, ne lie pas, ne gère pas les identifiants, et le 401 ne propose
plus `oidc`. Les refus typés du flux `reauth`, l'unicité de l'état, la comparaison de
`auth_time` (présent, en secondes UTC, borné par `max_age`, une minute d'avance tolérée) et
l'avis au titulaire (destination personnelle seule, écrit dans la transaction du changement)
ont été vérifiés sans objection.

La promesse est pourtant plus large que la liste : la décision dit « tout ce qui ajoute un
facteur, une clé, **un jeton** ou un lien », et docs/admin/oidc.md « créer une clé d'API ou un
jeton ». Deux familles d'actions créent un accès qui survit à la session sans porter aucun des
trois effets, donc sur la seule preuve du fournisseur, même quand le compte a un mot de passe :

- `device_token.issue` et `device_token.rotate` (`domain/catalogue.py`, effet
  `TOUCHES_SECRETS`) : un jeton d'appareil lit la bibliothèque du profil par OPDS et kosync,
  survit à la révocation de la session et à la déliaison du fournisseur, qui ne ferme que les
  sessions `proof = 'provider'` (`adapters/store.py`) ;
- `profile.create` et `profile.update` (effet `MANAGES_PROFILES`) : un compte d'administrateur
  au mot de passe choisi par l'appelant, ou la promotion d'un membre, survivent pareillement —
  alors que la même session, à la même seconde, se voit refuser `passkey.register` et
  `oidc.link_own`.

Les trous sont fermés (la règle suit les effets du catalogue, `domain/reserved.py`) : chaque PoC
est un test ordinaire, qui échoue si la preuve du fournisseur seule repasse.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from shelfhold.config import Settings
from tests.auth_env import PASSWORD
from tests.integration.test_m5_l03_oidc import LINK, World, world_for
from tests.integration.test_m5_l04_oidc_freshness import REAUTH, signed_in

pytestmark = pytest.mark.req("EF-USR-024")

DEVICE_TOKENS = "/api/v1/device-tokens"
DEVICES = "/api/v1/devices"
PROFILES = "/api/v1/profiles"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with world_for(monkeypatch, settings) as w:
        yield w


def proved_at_the_provider(world: World, browser: TestClient) -> None:
    """The session is fresh, and its proof is the provider's alone (the callback said so).

    The contrast the rule already keeps: the very same session cannot link an identity, one of
    the three guarded effects, and the 401 does not offer the provider again. Everything asked
    below is of the same kind — an access that outlives the session — so it must be refused the
    same way while the account has a local factor.
    """
    answer = world.send(browser, "POST", REAUTH)
    assert answer.status_code == 200, answer.text  # a linked account, a live session
    back = world.callback(browser, world.idp.authorize(str(answer.json()["authorization_url"])))
    assert back.headers["location"].endswith("?oidc=reauth")
    refused = world.send(browser, "POST", LINK)
    assert refused.status_code == 401 and "oidc" not in refused.json()["reauth_methods"]


# --- a device token, the durable way the rule does not know (device_token.issue) ----------------


def test_oidc_a_provider_proof_pairs_no_e_reader_of_an_account_with_a_password(
    world: World,
) -> None:
    """The account has a password, the session is fresh by the provider's word alone: pairing
    an e-reader would hand whoever holds the account at the provider a token that reads the
    library by OPDS, keeps working after the session is revoked and survives the unlink (it
    closes `provider` sessions only, never a token). The three guarded effects are refused this
    very proof; the token is a way in of the same kind and must be refused the same way."""
    mum = signed_in(world)
    proved_at_the_provider(world, mum)
    pairing = world.send(mum, "POST", DEVICE_TOKENS, {"name": "a reader", "kind": "koreader"})
    assert pairing.status_code == 401 and pairing.json()["code"] == "reauth_required"
    assert "oidc" not in pairing.json()["reauth_methods"]


def test_oidc_a_provider_proof_rotates_no_e_reader_secret_of_its_account(world: World) -> None:
    """The holder paired her e-reader from her own password session. Whoever holds her account
    at the provider signs in here, makes that same session fresh, and rotates the token: the new
    secret is shown once — to them — and the e-reader of the holder stops working. Fixing the
    pairing alone would leave this door open, so both routes carry the hole."""
    paired = world.send(world.mum, "POST", DEVICE_TOKENS, {"name": "a reader", "kind": "koreader"})
    assert paired.status_code == 201, paired.text  # the holder's own fresh session pairs it
    thief = signed_in(world, ip="198.51.100.31")  # the same account, by the provider
    proved_at_the_provider(world, thief)
    turned = world.send(thief, "POST", f"{DEVICES}/{paired.json()['device_id']}/rotate")
    assert turned.status_code == 401 and turned.json()["code"] == "reauth_required"
    assert "oidc" not in turned.json()["reauth_methods"]


# --- a new account or an admin role, durable too (profile.create, profile.update) --------------


def test_oidc_a_provider_proof_creates_no_admin_profile(world: World) -> None:
    """Whoever holds an administrator's account at the provider — the account here having a
    password, which is what `session.proof` is about — signs in, proves it fresh there, and
    writes a whole new admin profile with a password of their choosing. That account outlives
    the session, the revocation and the removal of the compromised link: a way in that no
    unlink closes, and strictly more than the passkey the same session cannot enrol."""
    boss = signed_in(world, "admin")
    proved_at_the_provider(world, boss)
    created = world.send(
        boss,
        "POST",
        PROFILES,
        {"username": "accomplice", "password": PASSWORD, "role": "admin"},
    )
    assert created.status_code == 401 and created.json()["code"] == "reauth_required"
    assert "oidc" not in created.json()["reauth_methods"]


def test_oidc_a_provider_proof_promotes_no_one_to_admin(world: World) -> None:
    """Same session, same proof: promoting a member hands their existing password the
    administration of the instance, for good. `MANAGES_PROFILES` is fresh in every profile
    precisely because a stolen session must not make an admin (`domain/reserved.py`) — but
    freshness by the provider alone is the one case the column was added to stop, and the
    promotion survives the removal of the compromised link, as the new account above does."""
    boss = signed_in(world, "admin")
    proved_at_the_provider(world, boss)
    promoted = world.send(boss, "PATCH", f"{PROFILES}/{world.ids['dad']}", {"role": "admin"})
    assert promoted.status_code == 401 and promoted.json()["code"] == "reauth_required"
    assert "oidc" not in promoted.json()["reauth_methods"]
