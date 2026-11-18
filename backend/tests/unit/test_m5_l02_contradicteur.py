"""Relecture adverse de M5 L02 (#347) : la migration 0100 est gelée sur la ligne mineure de M4.

`tools/published-migrations.toml` range `0100_device_registry.py` en `since = "1.1"`. M5 sort en
`v1.2.0` (PLAN-M5, L25) et `v1.1.0` n'est pas encore publiée (`v1.1.0-rc.2`). Avec ce gel, la garde
d'ENF-OPS-002 laisse passer la migration de la table `device` dans un correctif `v1.1.x` :
un correctif de M4 porterait le schéma de M5.

Vert depuis que l'entrée est en `since = "1.2"` (la PR attend `v1.1.0` et L00, ses dépendances de
carte).
"""

import pytest

from tests.tools_loader import REPO, load_tool

guard = load_tool("check_patch_migrations")

pytestmark = pytest.mark.req("ENF-OPS-002")

_FILE = "0100_device_registry.py"


def test_the_device_registry_migration_is_frozen_for_the_m5_minor_line() -> None:
    assert guard.load_frozen(REPO)[_FILE]["since"] == "1.2"


def test_a_patch_release_of_m4_refuses_the_device_registry_migration() -> None:
    assert any(_FILE in problem for problem in guard.check(REPO, "v1.1.1"))
