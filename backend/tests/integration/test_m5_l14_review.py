"""Relecture adverse de M5 L14 (EF-LEC-008) : « Reprendre la lecture » sur l'accueil.

Chaque trou trouvé en confrontant le lot à ses propres promesses est ici une preuve rouge en
`xfail(strict=True)` : elle échoue sur la branche pour la raison que son message dit, et devient
bruyante (XPASS) le jour où le trou est fermé, la marque ôtée dans le même commit. Ce que la même
lecture a trouvé solide reste couvert par les tests du lot et n'est pas refait ici : le filtre de
visibilité est celui de toutes les surfaces (une série cachée à un enfant n'est ni une entrée, ni
un compte, ni une erreur — réponse identique à celle d'un profil sans progression, tirage Kavita
compris, reclassement compris, édition cachée d'une œuvre visible comprise) ; le profil est
celui de la session (`?user_id=` est ignoré, une clé ou un jeton est refusé) ; un extrait
(D-P1) n'écrit rien (L07), donc ne fait apparaître aucune série ; l'ancre suit l'ordre du bloc
de série (`number_sort`, segment, rang de la source, identifiant) et reste dans l'Édition lue ;
deux sources d'accord sont une ligne par (profil, Unité), donc une entrée par série ; `limit`
est borné (1..24, 422 au-delà), au plus 120 séries examinées et 8 Unités essayées par série ;
`Cache-Control: no-store`, aucun ETag ; l'adresse de Kavita ne porte ni identifiants ni autre
schéma que http(s) ; le lien du lecteur est la paire du manifeste de L06 ; une Unité purgée
emporte ses positions avec elle (L07, second avis).
"""

from __future__ import annotations

from collections.abc import Iterator
from urllib.parse import urlsplit

import pytest

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_schema_storage_export import add_target

HOME = "/api/reader/continue"

# Un port hors plage : aucun écrivain de la ligne ne le contrôle (schéma, hôte, identifiants,
# requête et fragment sont vérifiés ; le port jamais), et aucun navigateur ne l'ouvre.
BAD_PORT_URL = "https://kavita.example.test:99999/"


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    """Le même monde que les tests du lot : la source coupe les extraits par le code du serveur."""
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


@pytest.mark.req("EF-LEC-008", "ENF-SEC-004")
def test_the_kavita_address_never_breaks_the_home_of_every_profile(world: World) -> None:
    """L'adresse de « Ouvrir dans Kavita » est relue dans la base à chaque réponse, comme une
    valeur dont on ne se souvient pas qu'elle fut validée (`kavita_open_url` ne regarde que le
    préfixe http(s)://). Or le port de cette adresse n'est contrôlé par aucun écrivain de la
    ligne : `_http_url` et `validate_target_url` vérifient le schéma, l'hôte, les identifiants,
    la requête et le fragment, jamais le port. La surface qui lit le même champ pour le même
    usage (les points d'entrée des liseuses) traite un port hors plage comme une adresse à
    refuser, pas à servir (« whose port is out of range gives None », `device_entry_points`,
    déjà éprouvée par `test_entry_points_refuse_anything_but_a_web_address`). Ici, `web_address`
    lit `parts.port` hors de son `try` : la valeur lève `ValueError` et `GET /api/reader/continue`
    casse pour tous les profils — et l'accueil, qui attend cette réponse dans sa `Promise.all`,
    reste sur son état de chargement, série lue ou non. Le contrat écrit de la fonction (« else
    None ») demande autre chose : une réponse 200, et jamais une adresse dont le port ne
    s'ouvre pas."""
    with write_transaction(world.seed.engine) as connection:
        add_target(connection, config={"url": BAD_PORT_URL})
    got = world.admin.get(HOME)
    assert got.status_code == 200, got.text  # une adresse inouvrable n'est pas une raison de casser
    handed = got.json()["kavita_url"]
    if handed is not None:  # « else None » : ce qui est rendu doit s'ouvrir dans un navigateur
        parts = urlsplit(handed)
        try:
            port = parts.port
        except ValueError:
            port = -1
        assert parts.scheme in ("http", "https") and (port is None or 0 < port < 65536), handed
