"""Relecture adverse de M5 L03 (#362) : connexion OIDC. PoC en `xfail` strict.

- MIN-A : pendant l'avis de restauration, les connexions par clé d'accès sont refusées (un
  identifiant retiré depuis la sauvegarde est de nouveau vivant) ; la connexion OIDC, autre entrée
  persistante qui survit à la session qui l'a ajoutée (`LINKS_IDENTITY`, « comme une clé
  d'accès »), passe. Avec la déliaison de L04, un `sub` délié après la sauvegarde revient.
- NIT-B : un `auth_time` hors des dates représentables fait lever `datetime.fromtimestamp` dans
  l'adaptateur : erreur 500 au retour au lieu du refus unique (le flux est consommé, la tentative
  n'est pas rendue).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa

from shelfhold.config import Settings
from shelfhold.infrastructure.restore_notice import RestoreNoticeData, write_notice
from tests.integration.test_m5_l03_oidc import World, refused, world_for

pytestmark = pytest.mark.req("EF-USR-004")


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> Iterator[World]:
    with world_for(monkeypatch, settings) as w:
        yield w


def _restore_pending(world: World) -> None:
    engine = sa.create_engine(f"sqlite:///{world.env.settings.database_path}")
    try:
        with engine.begin() as connection:
            write_notice(
                connection,
                RestoreNoticeData(
                    restored_at=world.env.clock.now().isoformat(),
                    backup_created_at=None,
                    sessions=0,
                    api_keys=0,
                ),
                keep_existing=False,
            )
    finally:
        engine.dispose()


def test_oidc_login_is_refused_while_a_restore_is_pending(world: World) -> None:
    assert world.link_admin("mum").status_code == 204
    _restore_pending(world)
    browser = world.browser()
    assert refused(world.sign_in(browser))
    assert world.signed_in_as(browser) is None


def test_oidc_auth_time_out_of_range_is_one_refusal(world: World) -> None:
    assert world.link_admin("mum").status_code == 204
    world.idp.tamper = {"auth_time": 10**20}
    answer = world.sign_in(world.browser())
    assert answer.status_code == 303  # a sign-in or the one refusal, never an error
