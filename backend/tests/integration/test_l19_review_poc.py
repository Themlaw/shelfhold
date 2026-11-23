"""Relecture adverse de M6 L19 : défauts confirmés, un test rouge chacun.

1. « Reprendre la lecture » ne voit plus une Unité couverte seulement par un volume exporté sans
   `content_id` : c'est la forme qu'écrit l'assemblage en volumes (`assembly._complete`,
   `content_id=None`). En M5, l'entrée disait « kavita » (correctif de l'E2E de M5 L24) ; depuis
   L19 elle disparaît, et le test de M5 qui la gardait a été réécrit sur une copie conservée.
2. `open_urls` est global et ne dépend que du type : il donne à tout profil l'adresse de chaque
   cible connectée, même sans compte à lui dessus, et « Ouvrir dans Kavita » ouvre la première
   cible du type, pas celle de la destination où se trouve son compte.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.db import write_transaction
from shelfhold.infrastructure.ids import new_ulid
from shelfhold.infrastructure.schema import export
from tests.integration.test_l05a_content_route import World, _world
from tests.integration.test_l19_read_elsewhere import sent
from tests.integration.test_m5_l06_publication import ServerCutArchives
from tests.integration.test_m5_l14_continue_reading import HOME, items, pulled


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    monkeypatch.setattr("tests.integration.test_l05a_content_route.FakeArchives", ServerCutArchives)
    yield from _world(monkeypatch, settings)


def _unit(world: World) -> str:
    from tests.integration.test_schema_catalog import add_unit

    with write_transaction(world.seed.engine) as connection:
        return str(add_unit(connection, world.seed.editions["ok"], "500"))


@pytest.mark.req("EF-LEC-008")
@pytest.mark.parametrize("state", ["written", "scanned", "resolved", "adopted"])
def test_poc_l19_an_assembled_volume_read_on_kavita_stays_to_continue(
    world: World, state: str
) -> None:
    """Un volume assemblé (export_item sans content_id) lu dans Kavita, compte vérifié : l'entrée
    reste et ouvre Kavita, comme en M5 (E2E de M5 L24)."""
    unit = _unit(world)
    item = sent(world, None, unit, readers=("admin",), state=state)
    pulled(world, "admin", "export_item", item, done=False)
    got = items(world.admin.get(HOME))
    assert [(i["unit_id"], i["target"]) for i in got] == [(unit, "kavita")]


@pytest.mark.req("EF-LEC-008")
def test_poc_l19_open_urls_names_no_target_the_profile_has_no_account_on(world: World) -> None:
    """La cible Komga où seul l'admin a un compte : sa réponse n'a rien à ouvrir pour l'enfant,
    elle ne lui donne pas l'adresse de ce serveur."""
    from tests.integration.test_m5_l07_positions import kept

    content, unit = kept(world)
    item = sent(world, content, unit, readers=("admin",), kind="komga", url="https://komga.test")
    pulled(world, "admin", "export_item", item, done=False)
    assert world.admin.get(HOME).json()["open_urls"] == {"komga": "https://komga.test"}
    kid = world.kid.get(HOME)
    assert kid.status_code == 200, kid.text
    assert kid.json()["open_urls"] == {}  # aucun compte à elle sur ce serveur


@pytest.mark.req("EF-LEC-008")
def test_poc_l19_open_in_opens_the_server_the_account_is_on(world: World) -> None:
    """Deux serveurs Kavita ; le compte de l'admin est sur le second : « Ouvrir dans Kavita »
    doit ouvrir celui-là, pas le premier connecté."""
    from tests.integration.test_m5_l07_positions import kept

    with write_transaction(world.seed.engine) as connection:  # a first Kavita, nothing of hers
        connection.execute(
            sa.insert(export.export_target).values(
                id="0" * 25 + "1", kind="kavita", name=f"k-{new_ulid()}",
                config={"url": "https://kavita-un.test"},
            )
        )  # fmt: skip
    content, unit = kept(world)
    item = sent(world, content, unit, readers=("admin",), url="https://kavita-deux.test")
    pulled(world, "admin", "export_item", item, done=False)
    got = world.admin.get(HOME).json()
    assert [(i["unit_id"], i["target"]) for i in got["items"]] == [(unit, "kavita")]
    assert got["open_urls"]["kavita"] == "https://kavita-deux.test"
