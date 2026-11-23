"""Troisième passage de la relecture adverse de L22 : le refus d'un lien familial donne la raison
d'un autre dossier.

Chaque dossier en attente répond désormais pour lui-même de sa **preuve**, mais la **raison**
gardée par l'état reste celle de toute la cible : le premier écart des dossiers sans étagère
liée. Ici le dossier du lien (`/manga/Kavita`) a son propre écart (un compte adulte sans
restriction lit sa bibliothèque et une autre), et une autre étagère familiale, que le Komga ne
mappe pas, donne `library_unmapped` : le refus dit `library_unmapped`, l'administrateur crée
une bibliothèque qui existe déjà au lieu de corriger le compte.
"""

from __future__ import annotations

import pytest

from shelfhold.jobs.daily_checks import komga_accounts_audit
from tests.export_env import add_second_shelf
from tests.family_env import family_env
from tests.fakes.komga import administrator, reader
from tests.integration.test_komga_access_audit import LIBRARY, kit, library, try_bind

OTHER = "0ADULTLIB01"


@pytest.mark.req("EF-EXP-047", "ENF-SEC-016")
def test_the_refusal_of_a_family_link_gives_the_reason_of_its_own_folder() -> None:
    with family_env(13) as env:
        add_second_shelf(env, max_age_rating=13)  # a family shelf Komga does not map: Kavita2
        fake, client, _auditor, events, target = kit(
            env, libraries=[library(), library(OTHER, "/adult")]
        )
        fake.users = [administrator(), reader("parent", [LIBRARY, OTHER])]  # no restriction
        komga_accounts_audit(env.engine, env.clock, events, lambda: [(target, client)])()
        refused = try_bind(env, target)
        assert refused is not None
        assert "account_unclassified" in refused, refused  # the gap of this folder, not another's
